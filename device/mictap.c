/* mictap.so —— 无侵入地把这台音箱的麦克风流拷一份出来（LD_PRELOAD 钩子）
 *
 * 目的：让 macmini 拿到实时麦克风音频。这不是"防网易关服"的保险，
 *       而是"说话中间能被打断 / 人类说话时不插话"的【硬前提】——
 *       只从 DBus 看事件的话，我们要等"最终识别结果"才知道有人说过话，
 *       那是秒级之后的事；打断要的是毫秒级。
 *
 * ★ 为什么不能直接抢声卡
 *   真麦克风阵列在 card1 / sndi2s1 / /dev/snd/pcmC1D0c，被 netease_voice 独占
 *   （arecord 报 Resource busy）。我们是 root，抢得过来 —— 但那会让网易的
 *   唤醒 / AEC / 云端识别全废。那是把音箱弄哑，不是给它加耳朵。
 *
 * ★ 为什么钩 snd_pcm_readi（这两个符号是 readelf 从设备二进制里读出来的，不是猜的）
 *   netease_voice 动态链接 libasound.so.2，导入表里明确有 snd_pcm_readi
 *   （而 mmap_begin/mmap_commit 的调用计数是 0 ⇒ 它没走 mmap 那套）。
 *   所以只要我们的 .so 也定义 snd_pcm_readi、且靠 LD_PRELOAD 排在它前面，
 *   它的调用就会先进我们这儿：拷一份发走，再原样转交真函数 —— 它一无所知。
 *
 * ★ 为什么钩 snd_pcm_hw_params 来拿格式（而不是 snd_pcm_hw_params_current）
 *   前者是 App【自己】调的那一刻（导入表里有），参数对象刚被填好，格式是确定的；
 *   后者虽然 alsa-lib 也导出，但那要靠我们主动去问一个别人可能还没配完的句柄。
 *   在 App 的调用点上顺手读，时序上不会有第二种种可能。
 *
 * ★ 为什么一个头文件都不 include、连 libc 都不链（-nostdlib）
 *   设备是 musl。带一份自己的 libc 进来，会把 malloc 之类符号顶到全局作用域、
 *   把主程序那份顶掉 —— 那是灾难性的。所以本文件所有外部符号
 *   （dlsym/socket/sendto/getenv…）都【只声明、不定义】，运行时由动态链接器
 *   从【进程自己那份 musl】里解析。已核实：musl 导出了我用的每一个。
 *
 * ★ 绝不阻塞音频线程
 *   UDP + MSG_DONTWAIT，socket 队列满了就丢包、绝不等待。这是麦克风通路，
 *   宁可丢我们的副本，也不能让网易那边卡住一帧。
 *
 * ★ 为什么要在 .so 里做「低通 + 抽取 + 高通」（这是实测逼出来的，不是优化洁癖）
 *   真机是 96000 Hz × 2 声道 × S24_LE = 每秒 768 KB 的持续上行，跑在拥挤的
 *   ch1 上。而且把真录音拿来量频谱，发现人声（300-3400 Hz）在 -85.7 dBFS，
 *   而【24-48 kHz 的超声垃圾在 -70.4 dBFS】—— 那是 96k 采样时钟的 1/3 分频
 *   串扰（32 kHz 附近一簇谱线），比人声高 15 dB。直接抽取到 16 kHz 的话，
 *   这段会整个【混叠折叠】回 0-8 kHz，正好糊在人声上。所以抗混叠低通是必须的。
 *   另外 29.3 / 46.9 / 50 Hz 那几根低频强线（也是 -75 dBFS 上下）不混叠、
 *   会原地留下，白占 14 dB 动态范围 —— 用隔直高通清掉。
 *
 *   一箭三雕：768 KB/s → 64 KB/s（砍 12 倍），同时把市电嗡声和那簇怪音一起铲了。
 *   实测（在真录音上跑定点实现）：人声带只掉 0.2 dB，25-35 Hz 掉 51 dB。
 *
 * ★ 为什么全用整数定点、一个浮点都不碰
 *   这份 .so 是 LD_PRELOAD 进别人进程的，任何【被自己带进来的】符号都会顶到
 *   进程全局作用域。浮点本身没问题（设备是 hard-float ABI，VFP 是硬件），
 *   但 double 会引 __aeabi_dadd 之类 libgcc 符号；变量除法会引 __aeabi_idiv。
 *   所以：系数全部 Q20 定点（int32 存、int64 累加，smull 是硬件指令），
 *   除法只走自己写的 udiv()。编译后用
 *     arm-linux-gnueabihf-nm -u mictap.so
 *   确认未定义符号只有 musl 里那几个，没有 __aeabi_*。
 *
 * 编译（在 macmini 上）：
 *   arm-linux-gnueabihf-gcc -O2 -fPIC -shared -nostdlib -lgcc \
 *       -o mictap.so mictap.c
 * 部署：★唯一目标 = 设备 rootfs 的 /lib/mictap.so。用 ./deploy_mictap.sh，别手推。
 *   ★★ 绝不能放 /mnt/UDISK：netease_voice 起于开机 5.60 秒，而 UDISK 要 8.2 秒才挂上，
 *   那一刻 UDISK 上的 .so 还不存在 ⇒ musl 的 ldso 【静默忽略】LD_PRELOAD（不报错、进程照跑）
 *   ⇒ 麦克风在跑但钩子根本不在，一个包都不发。判据 `grep mictap /proc/<pid>/maps`。
 *   /mnt/UDISK/spk/backup/ 下那份是【只读备份】，永不部署。详见 netease-vbox-boot-chain 坑②。
 *
 * 环境变量（都可选，不设就用默认值）：
 *   MICTAP_HOST   目标地址，默认 192.168.1.100（跑"大脑"那台机器的局域网地址）。
 *                 ★ 这是【你的部署】才定的值 —— 默认值只是示例，请在启动脚本里
 *                 用 MICTAP_HOST 环境变量指定你自己的机器（例：export MICTAP_HOST=10.1.2.3）。
 *                 走点对点隧道再填隧道地址也一样，本文件不关心它是哪个网段。
 *   MICTAP_PORT   目标端口，默认 9998
 *   MICTAP_RATE   探测失败时的兜底采样率，默认 16000
 *   MICTAP_CH     探测失败时的兜底声道数，默认 2
 *   MICTAP_FMT    探测失败时的兜底格式号（ALSA snd_pcm_format_t），默认 2 = S16_LE
 *   MICTAP_FILTER 置 0 关掉「低通+抽取+高通」，退回原始码流（安全阀，排查用）
 *   MICTAP_DEBUG  置 1 打开 /tmp/mictap.log（排查用；平时关掉）
 *
 * 急停开关：设备上存在 /tmp/spk/mictap.off 就整个不发（不用重启服务）。
 */

typedef unsigned int  u32;
typedef unsigned long ulong;
typedef long          slong;

/* ---------------------------------------------------------------- 外部符号
 * 全部只声明、不定义，运行时由动态链接器从进程自己的 musl / libasound 解析。
 * 已逐个核实存在：musl 导出 dlsym/getenv/socket/sendto/open/write/close/memcpy；
 * libasound 导出 snd_pcm_readi 与三个 hw_params getter。
 */
extern void *dlsym(void *handle, const char *name);
extern char *getenv(const char *name);
extern int   socket(int domain, int type, int protocol);
extern slong sendto(int fd, const void *buf, ulong len, int flags,
                    const void *addr, u32 addrlen);
extern int   open(const char *path, int flags, ...);
extern int   close(int fd);
extern slong write(int fd, const void *buf, ulong len);
extern int   read(int fd, void *buf, u32 len);            /* ★ 为唤醒词注入加的。
                                                           * musl 的签名是
                                                           * ssize_t read(int, void*, size_t)，
                                                           * 32 位 ARM 上 ssize_t=int、size_t=u32 */
extern void *memcpy(void *dst, const void *src, u32 n);   /* 第三参必须是 size_t = u32，
                                                           * ulong 类型对不上 gcc 内建声明 */

/* ---------------------------------------------------------------- 配置
 * MICTAP_HOST 没设时用的目标地址。
 * ★ 这是【你的部署】才定的值 —— 这里写的是示例地址（跑"大脑"那台机器的局域网地址），
 *   不是任何人的真实环境。改法二选一：
 *     ① 直接把下面这行改掉，重新编译；
 *     ② 不改这里，在【启动 netease_voice 的脚本里】export MICTAP_HOST=<你的地址>
 *        （推荐：这样一份 .so 可以配不同环境，不用重编）。
 *   见文件头「环境变量」那段。
 */
#define MICTAP_HOST_DEFAULT "192.168.1.100"

/* ---------------------------------------------------------------- 常量
 * 只抄用得到的这几个，避免为了几个数字去 include 一整套 glibc 头文件。
 */
#define RTLD_NEXT    ((void *)-1L)
#define AF_INET      2
#define SOCK_DGRAM   2
#define MSG_DONTWAIT 0x40
#define O_RDONLY     0
#define O_WRONLY     1
#define O_CREAT      0100
#define O_APPEND     02000
#define O_TRUNC      01000

/* 滤波器系数（Q20 定点，由 design_filter.py 从真录音里量出来设计并生成）。
 * 这是一份生成的代码，别手改 —— 要调就改脚本再跑一遍。 */
#include "mictap_filt.h"

#define MT_MAGIC     0x5041544du      /* 内存里就是 "MTAP" */
#define MT_VER       2u               /* 2 起：flags 高 24 位携带原始采样率 */
#define MT_MAXPKT    1400u            /* 一个 UDP 包的上限，别超 MTU 惹分片 */
#define MT_PAYLOAD   (MT_MAXPKT - 32u)

/* 滤波链的参数 */
#define MT_MAXCH     2                /* 会做滤波的最大声道数（真机是 2） */
#define MT_F_DECIM   1u               /* flags bit0：这份流是滤过 + 抽过的 */
#define MT_RATE_SH   8                /* flags 从 bit8 起放【原始】采样率 */
#define MT_OCHUNK    520              /* 一轮最多产出多少输出帧（见 filt_feed 的算账） */
#define MT_ICHUNK    (MT_OCHUNK * MT_DECIM)

/* 内核 ABI，各 libc 一致 —— 自己写一份省得 include <netinet/in.h> */
struct sa_in {
	unsigned short family;   /* AF_INET */
	unsigned short port;     /* 网络字节序 */
	u32            addr;     /* 网络字节序 */
	char           pad[8];
};

/* 包头：让接收端不用猜格式。32 字节，字段顺序改了要同步改 macmini 那边。 */
struct pkt_hdr {
	u32 magic;    /* MT_MAGIC */
	u32 ver;      /* MT_VER */
	u32 seq;      /* 递增序号，接收端用来认丢包 */
	u32 frames;   /* 本包帧数 */
	u32 rate;     /* 采样率 */
	u32 ch;       /* 声道数 */
	u32 fmt;      /* ALSA snd_pcm_format_t，接收端据此算位宽 */
	u32 flags;    /* 保留，现在恒 0 */
};

/* ---------------------------------------------------------------- 小工具 */

/* 不依赖 libgcc/libc 的整数除法。ARMv7-A 没有硬件除法指令，
 * 而 gcc 会为 `/` 生成 __aeabi_uidiv 调用 —— 虽然 musl 里有这个符号，
 * 但这份 .so 的价值在于"零依赖"，能不欠人情就不欠。 */
static ulong udiv(ulong a, ulong b)
{
	ulong q = 0;
	if (!b)
		return 0;
	while (a >= b) {
		ulong s = b, m = 1;
		while (s <= (a >> 1)) {   /* s*2 <= a，写成移位免得再触发除法 */
			s <<= 1;
			m <<= 1;
		}
		a -= s;
		q += m;
	}
	return q;
}

/* "192.168.1.100" → sin_addr 要的【网络字节序】值。
 * 注意：要的是内存里摆成 [c0][a8][01][64]，所以小端机上这个 u32 的值是 0x6401A8C0。 */
static u32 parse_ip(const char *s)
{
	u32 v = 0, n = 0;
	int part = 0, i = 0;

	if (!s || !s[0])
		return 0;
	for (;;) {
		char c = s[i];
		if (c >= '0' && c <= '9') {
			n = n * 10u + (u32)(c - '0');
			if (n > 255u)
				return 0;
			i++;
			continue;
		}
		if (c == '.' || c == 0) {
			if (part > 3)
				return 0;
			v |= n << (u32)(part * 8);
			part++;
			n = 0;
			if (c == 0)
				break;
			i++;
			continue;
		}
		return 0;
	}
	return (part == 4) ? v : 0;
}

static int env_long(const char *name, long dflt)
{
	char *p = getenv(name);
	long v = 0;
	int i = 0, neg = 0;

	if (!p || !p[0])
		return (int)dflt;
	if (p[0] == '-') { neg = 1; i = 1; }
	for (; p[i]; i++) {
		if (p[i] < '0' || p[i] > '9')
			return (int)dflt;
		v = v * 10 + (p[i] - '0');
		if (v > 1000000)
			return (int)dflt;
	}
	return (int)(neg ? -v : v);
}

/* ALSA snd_pcm_format_t → 每样本字节数。只列设备上真可能出现的那些，
 * 认不出来的按 2 字节（S16_LE，最保守的选择）。 */
static int fmt_width(int f)
{
	switch (f) {
	case 0:  case 1:  return 1;                       /* S8 / U8 */
	case 2:  case 3:  case 4:  case 5:  return 2;     /* S16_LE/BE, U16_LE/BE */
	case 6:  case 7:  case 8:  case 9:                /* S24/U24_LE/BE */
	case 10: case 11: case 12: case 13: return 4;     /* S32/U32_LE/BE */
	case 14: case 15: return 4;                       /* FLOAT_LE/BE */
	case 16: case 17: return 8;                       /* FLOAT64_LE/BE */
	case 18: case 19: return 4;                       /* IEC958_SUBFRAME */
	case 32: case 33: case 34: case 35: return 3;     /* S24_3LE/BE, U24_3LE/BE */
	case 36: case 37: case 38: case 39: return 3;     /* S20_3LE/BE, U20_3LE/BE */
	default: return 2;
	}
}

/* ---------------------------------------------------------------- 日志
 * 只在 MICTAP_DEBUG=1 时开。一个预加载的 .so 没有日志根本没法排查，
 * 但平时必须闭嘴：这是音频线程，每帧都写盘是自找麻烦。 */
static int logfd = -2;    /* -2 = 还没决定；-1 = 关着 */

static void logs(const char *s)
{
	ulong n = 0;

	if (logfd == -2) {
		char *p = getenv("MICTAP_DEBUG");
		logfd = (p && p[0] && p[0] != '0')
		        ? open("/tmp/mictap.log", O_WRONLY | O_CREAT | O_APPEND, 0644)
		        : -1;
	}
	if (logfd < 0)
		return;
	while (s[n])
		n++;
	write(logfd, s, n);
}

/* 打一行 "key=值" */
static void logkv(const char *k, long v)
{
	char buf[80];
	int i = 0, n = 0;
	char t[24];

	if (logfd == -2)
		logs("");
	if (logfd < 0)
		return;
	while (k[i] && i < 48)
		buf[i] = k[i], i++;
	if (v < 0) { buf[i++] = '-'; v = -v; }
	if (v == 0) {
		t[n++] = '0';
	} else {
		while (v > 0 && n < 23) { t[n++] = (char)('0' + (int)(v % 10)); v /= 10; }
	}
	while (n > 0 && i < 78)
		buf[i++] = t[--n];
	buf[i++] = '\n';
	write(logfd, buf, i);
}

/* ---------------------------------------------------------------- 每个 PCM 句柄的格式
 * 句柄是我们自己在 snd_pcm_hw_params 里记下来的，最多几个，线性找够了。 */
#define MT_MAXCAP 8

/* 高通那两级 biquad 的状态。用直接 I 型（x1/x2/y1/y2 分开存），
 * 不用直接 II 型：II 型的状态在定点下容易出极限环，I 型笨但稳。 */
struct hpst {
	int x1[2], x2[2], y1[2], y2[2];
};

struct cap {
	void *pcm;
	int   ok;      /* 格式已知 */
	u32   rate;
	u32   ch;
	int   fmt;
	int   w;       /* 每样本字节数 */

	/* ↓ 滤波链的状态。decim=0 表示这个句柄走原始透传（格式不认识时的退路） */
	int   decim;
	int   dw;                        /* FIR 延迟线的写入位置 */
	int   ph;                        /* 抽取相位计数，跨调用要保持 */
	struct hpst hp[MT_MAXCH];
	int   dl[MT_MAXCH][MT_FIRN];     /* FIR 延迟线，按声道分开 */
};

static struct cap caps[MT_MAXCAP];
static int ncap;

/* 单实例自旋锁：读的是几个全局表，用编译器内建原子就够，不必拉 pthread。 */
static volatile int mlock;

static void lock(void)   { while (__sync_lock_test_and_set(&mlock, 1)) ; }
static void unlock(void) { __sync_lock_release(&mlock); }

static struct cap *cap_get(void *pcm, int add)
{
	int i;

	for (i = 0; i < ncap; i++)
		if (caps[i].pcm == pcm)
			return &caps[i];
	if (!add || ncap >= MT_MAXCAP)
		return 0;
	caps[ncap].pcm = pcm;
	caps[ncap].ok = 0;
	return &caps[ncap++];
}

/* ---------------------------------------------------------------- 全局状态 */
static volatile int inited;
static int enabled = 1;
static int sock = -1;
static struct sa_in dst;
static u32 seq;
static int said_first;      /* 只报一次"第一个包发出去了" */
static int said_drop;       /* 只报一次"发不出去" */

/* ---------------------------------------------------------------- 初始化
 * 惰性：在第一次真正需要时才做。不在 constructor 里做，是因为 constructor
 * 跑得比 libc 完全就绪还早，那时调 getenv/socket 有风险。
 * 用 test-and-set 抢占：抢到的人干活并置 inited=1，别人看到 inited=1 就走，
 * 此时 sock 还是 -1，ship() 会直接丢这一个包 —— 丢一个包无所谓。 */
static void init_once(void)
{
	const char *phost;
	long port, rate, ch, fmt;
	u32 a;

	if (__sync_lock_test_and_set(&inited, 1))
		return;

	/* 兜底值：万一探测失败，至少还能出一条流（格式可能是错的，但能证明链路通） */
	rate = env_long("MICTAP_RATE", 16000);
	ch   = env_long("MICTAP_CH", 2);
	fmt  = env_long("MICTAP_FMT", 2);

	phost = getenv("MICTAP_HOST");
	port  = env_long("MICTAP_PORT", 9998);
	a = parse_ip(phost ? phost : MICTAP_HOST_DEFAULT);
	if (!a) {
		logs("MICTAP_HOST 解析失败，回退 " MICTAP_HOST_DEFAULT "\n");
		a = parse_ip(MICTAP_HOST_DEFAULT);
	}
	dst.family = AF_INET;
	dst.port   = (unsigned short)(((port & 0xff) << 8) | ((port >> 8) & 0xff));
	dst.addr   = a;

	/* ★ 这里【绝不能】往 caps[] 里写兜底值。
	 *   踩过：第一版在这儿把 caps[] 全部初始化成兜底格式，而 snd_pcm_hw_params
	 *   （探测，早）跑在第一次 readi（init_once，晚）之前 ⇒ 探测到的真格式刚写进去
	 *   就被这里的兜底值覆盖了，发出的包头写着 16000 而真流是 48000。
	 *   恰好 ch/w 都是 2 才没当场露馅 —— 最难查的就是这种。
	 *   兜底只走 on_frames() 里那个"没记到这个句柄"的 tmp 分支，一个地方就够。 */

	sock = socket(AF_INET, SOCK_DGRAM, 0);
	logs("--- mictap 启动 ---\n");
	logkv("dst=", (long)dst.addr);
	logkv("port=", port);
	logkv("sock=", sock);
	logkv("def_rate=", rate);
	logkv("def_ch=", ch);
	logkv("def_fmt=", fmt);
	if (sock < 0) {
		enabled = 0;
		logs("socket() 失败，整个关掉\n");
	}
}

/* ---------------------------------------------------------------- 发一包
 * 头 + 载荷拼在一个栈缓冲里一次 sendto 发走（分两次发的 syscall 开销翻倍）。 */
static void ship_fmt(int w, int nch, u32 rate, int fmt, u32 flags,
                     const unsigned char *p, ulong frames)
{
	unsigned char pkt[MT_MAXPKT];
	struct pkt_hdr h;
	ulong bpf, per, n, bytes;

	if (sock < 0)
		return;

	bpf = (ulong)w * (ulong)nch;
	if (!bpf)
		return;
	per = udiv((ulong)MT_PAYLOAD, bpf);
	if (!per)
		return;

	while (frames) {
		n = (frames < per) ? frames : per;
		bytes = n * bpf;

		h.magic  = MT_MAGIC;
		h.ver    = MT_VER;
		h.seq    = ++seq;
		h.frames = (u32)n;
		h.rate   = rate;
		h.ch     = (u32)nch;
		h.fmt    = (u32)fmt;
		h.flags  = flags;

		memcpy(pkt, &h, sizeof(h));
		memcpy(pkt + sizeof(h), p, bytes);

		if (sendto(sock, pkt, (ulong)sizeof(h) + bytes, MSG_DONTWAIT,
		           &dst, (u32)sizeof(dst)) < 0) {
			if (!said_drop) {
				said_drop = 1;
				logs("sendto 失败（网络不通？不重试，继续丢包）\n");
			}
		} else if (!said_first) {
			said_first = 1;
			logs("首个包已发出\n");
			logkv("  frames=", (long)n);
			logkv("  rate=", (long)rate);
			logkv("  ch=", (long)nch);
			logkv("  fmt=", (long)fmt);
			logkv("  flags=", (long)flags);
		}

		p += bytes;
		frames -= n;
	}
}

/* 原始透传那条路（格式不认识时的退路）：按句柄记下来的格式发。 */
static void ship(struct cap *c, const unsigned char *p, ulong frames)
{
	ship_fmt(c->ok ? c->w : 2, (int)(c->ok ? c->ch : 2u), c->rate, c->fmt, 0, p, frames);
}

/* ---------------------------------------------------------------- 滤波链
 * 24 位输入 → FIR 低通（抗混叠）→ 6 倍抽取 → 4 阶巴特沃斯高通 → int16 输出。
 * 全程 int64 累加、系数 Q20。没有一处浮点，也没有一处变量除法。
 */

/* 读一个样本，统一搬到【24 位域】（±2^23）。
 * ★ S24_LE（fmt=6，24 位左对齐塞在 4 字节里）和 S32_LE（fmt=10，满 32 位）
 *   在"取高 24 位"这件事上是【同一段代码】—— 都是 >>8，别写成两套。
 *   16 位的就 <<8 顶上来（精度不够但能跑）。 */
static int rd24(const unsigned char *p, int w)
{
	if (w == 4) {
		int v = (int)((u32)p[0] | ((u32)p[1] << 8) |
		              ((u32)p[2] << 16) | ((u32)p[3] << 24));
		return v >> 8;
	}
	return (int)(short)((u32)p[0] | ((u32)p[1] << 8)) << 8;
}

/* FIR 低通点积。dl 是环形延迟线，newest 是【最新样本】所在下标。
 * 累加必须 int64：|h| 最大约 2^20，|x| 最大 2^23，277 抽头合起来到 2^43。 */
static int fir_dot(const int *dl, int newest)
{
	long long acc = 0;
	int i = newest, k;

	for (k = 0; k < MT_FIRN; k++) {
		acc += (long long)mt_fir_q[k] * (long long)dl[i];
		if (i == 0)
			i = MT_FIRN;
		i--;
	}
	return (int)((acc + (1 << (MT_COEF_Q - 1))) >> MT_COEF_Q);
}

/* 两级 biquad 串联（4 阶巴特沃斯高通）。
 * ★ 这里【不能】用直接 II 型：定点下 II 型把两个极点状态挤在一起，
 *   没有输入也容易自激出极限环。I 型笨一点，但稳。 */
static int hp_run(struct hpst *s, int x)
{
	int st;

	for (st = 0; st < 2; st++) {
		const int *q = mt_hp_q[st];
		long long acc = (long long)q[0] * (long long)x
		              + (long long)q[1] * (long long)s->x1[st]
		              + (long long)q[2] * (long long)s->x2[st]
		              - (long long)q[3] * (long long)s->y1[st]
		              - (long long)q[4] * (long long)s->y2[st];

		s->x2[st] = s->x1[st];
		s->x1[st] = x;
		x = (int)((acc + (1 << (MT_COEF_Q - 1))) >> MT_COEF_Q);
		s->y2[st] = s->y1[st];
		s->y1[st] = x;
	}
	return x;
}

static int clip16(int v)
{
	if (v > 32767)
		return 32767;
	if (v < -32768)
		return -32768;
	return v;
}

/* 喂一段输入进去：边滤、边抽、边发。输出是 int16 交织。
 *
 * ★ 必须分块。调用方一次可能甩几千帧过来（ALSA period 大小不定），
 *   而输出缓冲是栈上的。按 MT_ICHUNK 切，每块最多产出 MT_OCHUNK 帧 ——
 *   算账：n ≤ MT_ICHUNK = MT_OCHUNK*6，相位 ph ≤ MT_DECIM-1 = 5，所以
 *   产出 (ph+n)/MT_DECIM ≤ (5 + MT_OCHUNK*6)/6 = MT_OCHUNK + 0.83，取整正好 MT_OCHUNK。
 *   （就是刚好够，没有富余 —— 要动这三个宏的话把这条算账重做一遍。） */
static void filt_feed(struct cap *c, const unsigned char *p, ulong frames)
{
	short out[MT_OCHUNK * MT_MAXCH];
	ulong inb = (ulong)c->w * (ulong)c->ch;      /* 输入一帧多少字节 */
	int nch = (int)c->ch;
	int ch;

	if (nch > MT_MAXCH)                          /* 兜底，正常到不了这儿 */
		nch = MT_MAXCH;

	while (frames) {
		ulong n = (frames < (ulong)MT_ICHUNK) ? frames : (ulong)MT_ICHUNK;
		int m = 0;
		ulong i;

		for (i = 0; i < n; i++) {
			const unsigned char *q = p + i * inb;

			/* 把这一帧写进各声道的延迟线（写完最新样本就落在 dw 上） */
			for (ch = 0; ch < nch; ch++)
				c->dl[ch][c->dw] = rd24(q + (ulong)ch * (ulong)c->w, c->w);

			if (++c->ph >= MT_DECIM) {
				c->ph = 0;
				for (ch = 0; ch < nch; ch++) {
					int y = fir_dot(c->dl[ch], c->dw);
					y = hp_run(&c->hp[ch], y);
					out[m * nch + ch] = (short)clip16((y + 128) >> 8);
				}
				m++;
			}
			if (++c->dw >= MT_FIRN)
				c->dw = 0;
		}

		if (m)
			ship_fmt(2, nch, (u32)MT_FS_OUT, 2,
			         MT_F_DECIM | ((u32)c->rate << MT_RATE_SH),
			         (const unsigned char *)out, (ulong)m);
		p += n * inb;
		frames -= n;
	}
}

/* 急停开关：设备上放一个 /tmp/spk/mictap.off 就立刻停，不用重启 netease_voice。
 * 排查时这比改环境变量重启服务方便得多 —— 而重启服务会打断网易的识别。 */
static int tap_off(void)
{
	int fd = open("/tmp/spk/mictap.off", O_RDONLY);
	if (fd >= 0) {
		close(fd);
		return 1;
	}
	return 0;
}

/* ---------------------------------------------------------------- 唤醒词注入
 * ★ 为什么需要它：唤醒词是设备【本地】的 libduilite 听着麦克风认的。想让音箱
 *   被"它自己"唤醒、又不依赖人开口，唯一的路就是直接喂它的耳朵 —— 而它的耳朵
 *   正是 snd_pcm_readi（本文件钩的就是这个函数）。
 *
 * ★ 为什么这样能绕开 AEC：回声消除拿【扬声器正在播的信号】当参考去减。我们这
 *   条路不经过扬声器、不占声卡、不产生声波 —— 参考信号里根本没有它，也就无从减起。
 *
 * ★ 为什么不替换 real() 而是覆盖 buf：先照常调真函数，保住它的阻塞与节流时序，
 *   唤醒引擎的取数节奏与平时逐帧一致；只把内容换掉，它察觉不到。
 *
 * ★ 为什么默认关：只有注入文件存在时才生效。文件不在，这条路径一个字节都不走 ——
 *   与改动前的行为完全一致（"别整坏"的底线）。
 *
 * ★ 保险丝：最多注入 MT_INJ_MAXSEC 秒就自己停。万一忘了收手，最坏也只是"唤醒
 *   引擎多听了 10 秒"，不会变成一个永远在自说自话的音箱。
 */
/* ★ 上限从 1MB 抬到 8MB（2026-09-21）：1MB 只装得下 1.365 秒，是按"注入一句
 *   唤醒词"定的。可一句唤醒词本身就要 1.87 秒 —— 一旦想注入【唤醒词+停顿+命令】
 *   这样一整段话，文件被【静默截断】到前 1.365 秒，剩下的循环重放：
 *   症状是"唤醒成功了、录音也有，可 ASR 永远识别不出命令"。
 *   8MB = 10.9 秒，够一整句话；设备 512MB 内存，这点 BSS 不算什么。 */
#define MT_INJ_MAX    (8u * 1024u * 1024u)
#define MT_INJ_MAXSEC 10                /* 最多注入这么多秒 */

static unsigned char inj_buf[MT_INJ_MAX];
static long inj_len;        /* 已载入字节数；0 = 没载入 */
static long inj_pos;        /* 播到哪了 */
static long inj_frames;     /* 已注入多少帧（保险丝用） */
static int  inj_on;         /* 1 = 正在注入 */
static int  inj_sync_logged;/* 序号起点只记一次 */
static long inj_probe;      /* 距上次尝试载入过了多少次取数（别每帧都 open） */

static const char *inj_path(void)
{
	char *p = getenv("MICTAP_INJECT");
	return (p && p[0]) ? p : "/tmp/mictap_inject.pcm";
}

/* 把注入文件读进内存。只做一次；成功就开闸。 */
static void inj_try_load(void)
{
	int fd;
	long n = 0;
	int r;

	if (inj_len)
		return;
	/* ★ 每 100 次取数才试一次 open。绝不写成"试够几次就永久放弃"：注入文件是
	 *   部署【之后】才推上去的，若进程头几毫秒试满就锁死，机制将永远醒不过来，
	 *   而症状是"什么都没发生"—— 最难查的那一类。100 次 ≈ 一秒内必定发现新文件。 */
	if (++inj_probe < 100)
		return;
	inj_probe = 0;
	fd = open(inj_path(), O_RDONLY);
	if (fd < 0)
		return;
	while ((ulong)n < sizeof(inj_buf)) {
		r = read(fd, inj_buf + n, (u32)(sizeof(inj_buf) - (ulong)n));
		if (r <= 0)
			break;
		n += r;
	}
	close(fd);
	if (n <= 0)
		return;
	inj_len = n;
	inj_pos = 0;
	inj_frames = 0;
	inj_on = 1;
	inj_sync_logged = 0;
	logkv("注入载入 字节", n);
}

/* 把 buf 换成本地录来的唤醒词。格式对不上就放弃 —— 宁可什么都不做，也绝不喂
 * 错格式的噪音给唤醒引擎（那等于对着它放广播）。 */
/* 写一个注入样本：拷 inj_buf 的 4 字节，再把序号塞进第 8~11 位。
 * ★ 必须先清掉这 4 位（~0xF00）再 OR —— 不能假设源里一定是 0。 */
static void inj_put(unsigned char *dst, const unsigned char *src, u32 id)
{
	u32 v = (u32)src[0] | ((u32)src[1] << 8) |
	        ((u32)src[2] << 16) | ((u32)src[3] << 24);

	v = (v & ~0xF00u) | ((id & 0xFu) << 8);
	dst[0] = (unsigned char)v;
	dst[1] = (unsigned char)(v >> 8);
	dst[2] = (unsigned char)(v >> 16);
	dst[3] = (unsigned char)(v >> 24);
}

static void inj_fill(void *pcm, void *buf, ulong frames)
{
	struct cap *c;
	unsigned char *p = (unsigned char *)buf;
	ulong bytes, i;
	u32 idx, base;

	if (!inj_on)
		return;
	c = cap_get(pcm, 0);
	if (!c || !c->ok || c->ch != 2 || c->w != 4) {
		inj_on = 0;
		logkv("注入放弃 声道", c ? (long)c->ch : -1L);
		return;
	}
	bytes = frames * 8u;
	if (inj_pos + (long)bytes > inj_len) {
		/* ★ 一轮放完就收（2026-09-21 改）：注入的语义是"替用户说一句完整的话"。
		 *   循环重放会让唤醒词紧接着再响一遍 ⇒ 多出一轮没人要的对话，
		 *   测的时候极难分辨"是我触发的还是它自己在循环"。
		 *   真要重复，重新推文件 + 重启即可。 */
		inj_on = 0;
		logkv("注入播完一轮 帧", inj_frames);
		return;
	}

	/* ★★★ 序号同步（2026-09-21 查出）—— 这是"ALSA 层完美、fespa 却只听到
	 *   房间噪声"的真凶。设备的每个 4 字节样本【第 8~11 位嵌着一个 1..12 循环
	 *   的序号】，逐样本 +1（实测 4194304 字节：+1 占 11/12，12→1 的回绕占 1/12）。
	 *   netease_voice 的 CheckData 会逐样本校验"上一个+1"，不合规的一律不要。
	 *   我们原先写 a16<<16（那些位恒 0）⇒ 每个样本都校验失败 ⇒ 整段被丢。
	 *   这里在覆盖【之前】先从真实样本里读出当前相位，再按同样步进写进去。
	 *   每个缓冲都重新同步一次 ⇒ 即使某次 frames 不是 6 的整数倍也自愈。 */
	idx = (u32)p[0] | ((u32)p[1] << 8) | ((u32)p[2] << 16) | ((u32)p[3] << 24);
	idx = (idx >> 8) & 0xFu;
	if (idx < 1u || idx > 12u)
		idx = 1u;                       /* 探不到合法相位就从 1 起；绝不能写 0 */
	if (!inj_sync_logged) {
		inj_sync_logged = 1;
		logkv("注入序号起点", (long)idx);
	}

	for (i = 0; i < frames; i++) {
		base = idx - 1u + (u32)(i * 2u);        /* 该帧左声道在交织流里的序号 */
		inj_put(p + i * 8u, inj_buf + inj_pos + (long)(i * 8u),
		        (base % 12u) + 1u);             /* 左 */
		inj_put(p + i * 8u + 4u, inj_buf + inj_pos + (long)(i * 8u) + 4,
		        ((base + 1u) % 12u) + 1u);      /* 右 */
	}

	inj_pos += (long)bytes;
	inj_frames += (long)frames;
	if (inj_frames > (long)MT_INJ_MAXSEC * 96000L) {   /* 保险丝 */
		inj_on = 0;
		logkv("注入自动停止 帧", inj_frames);
	}
}

static void on_frames(void *pcm, const void *buf, ulong frames)
{
	struct cap *c;
	unsigned char *p = (unsigned char *)buf;

	if (!frames || !buf)
		return;
	if (!inited)
		init_once();
	if (!enabled || sock < 0)
		return;
	if (tap_off())
		return;

	lock();
	c = cap_get(pcm, 0);
	if (c && c->ok) {
		int dec = c->decim;
		unlock();
		if (dec)
			filt_feed(c, p, frames);
		else
			ship(c, p, frames);
		return;
	}
	unlock();
	/* 没记到这个句柄（罕见）：按兜底格式发，宁可能听也别哑着 */
	{
		struct cap tmp;
		tmp.ok = 0;
		tmp.rate = (u32)env_long("MICTAP_RATE", 16000);
		tmp.ch   = (u32)env_long("MICTAP_CH", 2);
		tmp.fmt  = env_long("MICTAP_FMT", 2);
		tmp.w    = fmt_width(tmp.fmt);
		ship(&tmp, p, frames);
	}
}

/* ================================================================ 被钩的两个函数 */

/* --- snd_pcm_hw_params：App 自己调的那一刻，顺手把格式记下来 ---
 * 签名：int snd_pcm_hw_params(snd_pcm_t *pcm, snd_pcm_hw_params_t *params)
 * 成功之后 params 里就是最终生效的值，用三个 getter 读出来即可。 */
typedef int (*fn_hwparams)(void *pcm, void *params);
typedef int (*fn_getu)(const void *params, u32 *val);
typedef int (*fn_getrate)(const void *params, u32 *val, int *dir);
typedef int (*fn_getfmt)(const void *params, int *val);
typedef slong (*fn_readi)(void *pcm, void *buf, ulong frames);

static fn_getu    p_getch;
static fn_getrate p_getrate;
static fn_getfmt  p_getfmt;

static void probe(void *pcm, void *params)
{
	u32 rate = 0, ch = 0;
	int dir = 0, fmt = -1, w, dec = 0;

	if (!p_getch || !p_getrate || !p_getfmt || !params)
		return;
	if (p_getch(params, &ch) < 0 || ch == 0 || ch > 32)
		return;
	if (p_getrate(params, &rate, &dir) < 0 || rate < 1000 || rate > 384000)
		return;
	if (p_getfmt(params, &fmt) < 0 || fmt < 0)
		return;
	w = fmt_width(fmt);

	lock();
	{
		struct cap *c = cap_get(pcm, 1);
		if (c) {
			int i2, k;

			c->rate = rate;
			c->ch   = ch;
			c->fmt  = fmt;
			c->w    = w;
			c->ok   = 1;

			/* 什么时候走滤波链：采样率正好是被抽取的那一档、样本宽度认识、
			 * 声道数在能力内、且没被 MICTAP_FILTER=0 关掉。
			 * 对不上就走原始透传 —— 宁可能听也别哑着。 */
			c->decim = (rate == (u32)(MT_FS_OUT * MT_DECIM)
			            && (w == 4 || w == 2)
			            && ch >= 1 && ch <= (u32)MT_MAXCH
			            && env_long("MICTAP_FILTER", 1) != 0) ? 1 : 0;
			dec = c->decim;

			/* ★ 状态必须清零。换个格式重新探测时，上一个流的尾巴留在
			 *   延迟线里会变成一个"咔"声；IIR 状态更会直接发散。
			 *   这个循环只在 probe 时跑（罕见），不在音频路径上。 */
			c->dw = 0;
			c->ph = 0;
			for (i2 = 0; i2 < MT_MAXCH; i2++) {
				for (k = 0; k < MT_FIRN; k++)
					c->dl[i2][k] = 0;
				for (k = 0; k < 2; k++) {
					c->hp[i2].x1[k] = c->hp[i2].x2[k] = 0;
					c->hp[i2].y1[k] = c->hp[i2].y2[k] = 0;
				}
			}
		}
	}
	unlock();

	logs("探测到 PCM 格式\n");
	logkv("pcm=", (long)(ulong)pcm);
	logkv("  rate=", (long)rate);
	logkv("  ch=", (long)ch);
	logkv("  fmt=", (long)fmt);
	logkv("  位宽=", (long)w);
	logkv("  走滤波链=", (long)dec);
	if (dec) {
		logkv("  →输出 rate=", (long)MT_FS_OUT);
		logkv("  →输出 fmt=", 2L);
		logkv("  →每声道省=", (long)MT_DECIM);
	}
}

int snd_pcm_hw_params(void *pcm, void *params)
{
	static fn_hwparams real;
	int r;

	if (!real)
		real = (fn_hwparams)dlsym(RTLD_NEXT, "snd_pcm_hw_params");
	if (!real)
		return -38;                    /* -ENOSYS */

	if (!p_getch)
		p_getch = (fn_getu)dlsym(RTLD_NEXT, "snd_pcm_hw_params_get_channels");
	if (!p_getrate)
		p_getrate = (fn_getrate)dlsym(RTLD_NEXT, "snd_pcm_hw_params_get_rate");
	if (!p_getfmt)
		p_getfmt = (fn_getfmt)dlsym(RTLD_NEXT, "snd_pcm_hw_params_get_format");

	r = real(pcm, params);
	if (r == 0)
		probe(pcm, params);            /* ★ 只有成功才读，失败时 params 里的值是没意义的 */
	return r;
}

/* --- 原始字节转储（★ 排障用，只在 MICTAP_RAWDUMP 指了路径时才开）---
 *
 * 为什么需要它：CheckData 的反汇编证明，设备 ALSA 流的每个 4 字节样本里
 * 【第 8~11 位嵌着一个 4 位序号】，它逐样本校验"上一个+1"并按序号重排。
 * 我们注入的 a16<<16 把低 16 位全写成 0（序号恒 0）⇒ 整段被丢掉。
 * 要照着真样写序号，就得先看清真样本低 16 位长什么样 —— 而这个信息
 * 【只存在于注入之前】的原始缓冲里（mictap 发往 macmini 的那路已经滤过、看不到了）。
 */
#define MT_RAWMAX (4u * 1024u * 1024u)

static int   raw_fd = -2;               /* -2=没探过  -1=不开  >=0=正在写 */
static ulong raw_n;

static void raw_dump(void *pcm, void *buf, ulong frames)
{
	struct cap *c;
	const char *p;
	ulong bytes;

	if (raw_fd == -1)
		return;
	if (raw_fd == -2) {
		p = getenv("MICTAP_RAWDUMP");
		if (!p || !p[0]) {
			raw_fd = -1;
			return;
		}
		raw_fd = open(p, O_WRONLY | O_CREAT | O_TRUNC, 0666);
		if (raw_fd < 0) {
			raw_fd = -1;
			return;
		}
	}
	if (raw_n >= MT_RAWMAX)
		return;
	c = cap_get(pcm, 0);
	if (!c || !c->ok)
		return;
	bytes = frames * (ulong)c->w * (ulong)c->ch;
	if (bytes > MT_RAWMAX - raw_n)
		bytes = MT_RAWMAX - raw_n;
	write(raw_fd, buf, bytes);
	raw_n += bytes;
	if (raw_n >= MT_RAWMAX) {
		close(raw_fd);
		raw_fd = -1;
		logkv("原始转储完成 字节", (long)raw_n);
	}
}

/* --- snd_pcm_readi：真正的取数点 ---
 * ★ 这个函数在 ALSA 里【只用于 capture】，播放走的是 writei。
 *   所以"见到 readi 就拷"不会误伤播放，也正因如此才不用再去钩 snd_pcm_open。 */
slong snd_pcm_readi(void *pcm, void *buf, ulong frames)
{
	static fn_readi real;
	slong got;

	if (!real)
		real = (fn_readi)dlsym(RTLD_NEXT, "snd_pcm_readi");
	if (!real)
		return -38;

	got = real(pcm, buf, frames);

	/* ★ 原始转储必须在注入【之前】：我们要的是硬件真样本，不是我们写进去的。
	 *   不推注入文件时 raw_dump 更是唯一能看到真样本的窗口。 */
	if (got > 0)
		raw_dump(pcm, buf, (ulong)got);

	/* ★ 唤醒词注入：先让真函数照常取数（保住时序），再把内容换掉。
	 *   只在注入文件存在时生效；文件不在，这里一个字节都不动。 */
	if (got > 0) {
		if (!inj_len)
			inj_try_load();
		if (inj_on)
			inj_fill(pcm, buf, (ulong)got);
	}

	/* ★ 只认返回值：真的读到了多少帧，就报多少帧。
	 *   拿请求的 frames 去发会发出未初始化的缓冲区内容（那是垃圾）。 */
	if (got > 0)
		on_frames(pcm, buf, (ulong)got);

	return got;
}
