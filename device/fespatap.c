/* fespatap.c —— 观测 duilite_fespa_feed 到底收到了什么音频。
 *
 * 为什么需要它：
 *   mictap 证明了我们注入 ALSA 缓冲的内容，内容/音高/速度/电平全对
 *   （录回的上行流与素材包络相关 1.000、1.00 秒周期自相关 0.847、峰值 3794）；
 *   但设备就是不唤醒。也就是说，**问题在 ALSA 之后、唤醒判定之前**。
 *   中间隔着 duilite 的 fespa（AEC/降噪/唤醒一体，配置 words=di da di da）。
 *   所以把 fespa 的入口拦下来，看它拿到的到底是我们的唤醒词、还是被处理没了。
 *
 * 铁律：**只观测，不干预**。算完统计量就把参数【原样】转给真函数，
 *       绝不修改 data 的内容、绝不改 len、绝不改返回值。
 *
 * 编译（跟 mictap 同一套约束，见 deploy_mictap.sh）：
 *   arm-linux-gnueabihf-gcc -O2 -fPIC -shared -nostdlib -lgcc -Wall -Wextra \
 *       -o fespatap.so fespatap.c
 * ★ -nostdlib 是必须的：带 libc 会把主程序的 malloc 顶掉。
 * ★ 因此不能出现 __aeabi_* 符号 —— 所以【一次除法都不能写】：
 *   常量除数 GCC 会自己化成乘加，但【变量除数】会生成 __aeabi_uidiv。
 *   故 ms（均方）的除法留给离线做，这里只记 sum(v*v) 和样本数。
 */
typedef unsigned int   u32;
typedef unsigned long  ulong;
typedef long           slong;

extern void  *dlsym(void *h, const char *n);
extern int    open(const char *p, int flags, ...);
extern slong  write(int fd, const void *b, ulong n);
extern char  *getenv(const char *k);

#define O_WRONLY 1
#define O_CREAT  64
#define O_TRUNC  512

#define RTLD_NEXT ((void *)-1L)

#define MT_DUMPMAX (24u * 1024u * 1024u)   /* 原始音频最多存 24MB，内存盘别撑爆 */
#define MT_LOGMAX  1400                    /* 日志行数上限，防止跑一夜撑爆 /tmp */

typedef int (*fn_feed)(void *inst, void *data, int len);

static fn_feed real_feed;
static int     logfd = -1;
static int     rawfd = -1;
static long    ncall;
static ulong   rawn;

/* ---- 极简整数转字符串（无 libc，无除法） ---- */
static int fmtnum(char *o, long v)
{
	char t[24];
	int  n = 0, i = 0;
	ulong u = (v < 0) ? (o[i++] = '-', (ulong)(-v)) : (ulong)v;

	if (!u)
		t[n++] = '0';
	while (u) {
		t[n++] = (char)('0' + (int)(u % 10u));   /* 常量除数：GCC 化成乘加，不引 __aeabi */
		u /= 10u;
	}
	while (n)
		o[i++] = t[--n];
	return i;
}

static void logkv(const char *k, long v)
{
	char b[80];
	int  i = 0;

	if (logfd < 0)
		return;
	while (*k && i < 56)
		b[i++] = *k++;
	b[i++] = '=';
	i += fmtnum(b + i, v);
	b[i++] = '\n';
	write(logfd, b, (ulong)i);
}

static void loginit(void)
{
	logfd = open("/tmp/fespatap.log", O_WRONLY | O_CREAT | O_TRUNC, 0666);
	rawfd = open("/tmp/fespa_feed.raw", O_WRONLY | O_CREAT | O_TRUNC, 0666);
}

/* ★ 被 netease_voice 通过 PLT 调用（objdump 确认：r0=实例, r1=data, r2=len，
 *   调用点在 Netease_nduilite_writeaudio 里，每次 1536 帧）。
 *   LD_PRELOAD 的库在符号查找顺序里排在前面 ⇒ 这个定义会先被用到，
 *   真正的实现在 libduilite.so 里，靠 RTLD_NEXT 找到。 */
int duilite_fespa_feed(void *inst, void *data, int len)
{
	const short *s;
	long i, n;
	long long acc = 0;
	long peak = 0;

	if (!real_feed) {
		real_feed = (fn_feed)dlsym(RTLD_NEXT, "duilite_fespa_feed");
		if (!real_feed)
			return -1;
	}
	if (logfd < 0)
		loginit();

	ncall++;

	if (data && len >= 2) {
		n = len / 2;                     /* 按 16 位看；format 未知，先看电平量级 */
		if (n > 8192)
			n = 8192;
		s = (const short *)data;
		for (i = 0; i < n; i++) {
			long v = s[i];
			if (v < 0)
				v = -v;
			if (v > peak)
				peak = v;
			acc += (long long)v * (long long)v;
		}
		if (ncall <= MT_LOGMAX) {
			logkv("call", ncall);
			logkv("  len", (long)len);
			logkv("  peak", peak);
			logkv("  sumsq", (long)(acc >> 10));   /* 除以 1024 免得数字太大；离线再还原 */
		}
		if (rawn < MT_DUMPMAX) {
			write(rawfd, data, (ulong)len);
			rawn += (ulong)len;
		}
	} else if (ncall <= MT_LOGMAX) {
		logkv("call", ncall);
		logkv("  len", (long)len);
		logkv("  data空", data ? 0 : 1);
	}

	return real_feed(inst, data, len);       /* ★ 一个字节都不动 */
}
