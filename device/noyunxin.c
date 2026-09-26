/* noyunxin.c —— 把网易云信 SDK 的「登录 / 重连」两个入口堵死
 *
 * ============================ 为什么 ============================
 * 2026-09-21 我们把云信的 link / LBS / HTTPDNS 全钉到 127.0.0.1
 * （目的是不让唤醒后的音频出家门，见 /etc/hosts 里那批 127.0.0.1 条目）。
 *
 * 结果：SDK 连不上 —— 但它【每 50 秒徒劳重试一次】，15 小时没停过：
 *     err_code 415 → "Code != 200 and 20002, just do relogin!" → err_code 200
 *     → 3 秒后 "Yunxin not login, do not send msg!"
 * 代价：
 *   · 设备埋点 /tmp/sys_point_mark.log 每 50 秒写一次
 *     {"desc":"wifi disconnect, reconnect!","event":"B241"} + B114
 *   · 面板亮红灯（它把"够不到云"表达成了"wifi 断了"）
 *   · 白白烧 CPU（设备 load 6+，4 核）
 *
 * 而「音箱永远连不上云信」是【既定事实】——
 *   所以这不是去修网络，是让它别再试。
 * ★ 铁律：netease_voice 不能杀 —— 它身上挂着麦克风截流
 *   （LD_PRELOAD=/lib/mictap.so:/lib/fespatap.so，把实时麦克风拷一份发 macmini:9998）。
 *   杀了音箱就聋了。所以只在它内部把云信掐掉。
 *
 * ============================ 怎么堵 ============================
 * ★ 本文件【只定义两个符号】，其余一律让真库去答：
 *     LD_PRELOAD 的 noyunxin.so 排在查找链最前 ⇒ netease_voice 的 PLT 把
 *     nim_client_login / nim_client_relogin 解析到我们这两个空函数；
 *     剩下 17 个 nim_client_* 照旧从 /usr/lib/libnim.so 解析。
 *   ⇒ 不用 dlsym(RTLD_NEXT)、不用 -ldl、不用 libc，-nostdlib 就够（同 mictap.so）。
 *
 * ★ 故意【不】stub nim_client_init：
 *     先让 SDK 正常初始化（后续 reg_*_cb 之类才有合法状态可挂）；
 *     但只要不 login，它【没有任何东西可连】，链路根本不会起。
 *     连 init 一起空掉的话，后面对未初始化句柄的调用有踩空风险。
 *
 * ★ ABI 安全性：只让它 return 0，不声明参数。
 *     ARM EABI 下 r0=0 对 void 调用者无害、对 int 调用者是「成功」；
 *     这两个函数不可能返回结构体（那才会走隐藏指针、才真有 ABI 风险）。
 *
 * ============================ 判据 / 回退 ============================
 * ★★ 判据必须是【累计型】的，两条一起看（2026-09-23 实测值见下）：
 *     a) grep -c relogin /tmp/netease_voice_<pid>.log      —— 重试刷屏停没停
 *     b) top -b -n 1 里 netease_voice 的 %CPU              —— 白烧的 CPU 降没降
 *   实测：改前 relogin 1824 行 / 总 4265 行、CPU 48%；
 *         改后 relogin 0 行 / 总 43 行、CPU 33%。
 *
 * ★★★ 别拿 /tmp/sys_point_mark.log 的 B241 当判据 —— 我一度这么设计，【已证伪】：
 *   那个文件【埋点上传成功后会被清空】（亲眼见它从 12 行变 0 行）⇒ "B241 不再增长"
 *   既可能是"云信停了"，也可能只是"刚被清过"，判不出来。同理别用"灯变没变"。
 * ★ 灯本来就不一定跟着变：云信是【永不登录】了，不是【登录成功】了。
 *   灯若还红，那是另一件事（LED 显示层），另说。
 *
 * 回退：从 /etc/init.d/netease_voice_service 的 LD_PRELOAD 里删掉 /lib/noyunxin.so
 *       → 重启 netease_voice_service。★ 重启前必须验 dbus 前置条件（否则 init_dbus
 *       会走 reboot -f 硬重启音箱，见 deploy_mictap.sh ⑤）。
 *
 * 核实加载（和 mictap 同一套判据）：
 *     grep -c noyunxin /proc/$(pidof netease_voice)/maps   # 0 = 没加载
 *     ★ 只放 /lib（rootfs）——UDISK 挂载晚于 netease_voice 启动，
 *       放 UDISK 会被 musl 静默忽略（血泪见 deploy_mictap.sh 文件头）。
 */

/* ★ 只写 return 0 —— 别加参数、别加返回类型细节。
 *   调用者的实际签名（可能带一堆 const char* / 回调）与我们无关：
 *   r0-r3 和栈上的参数我们一个都不读，r0 回 0 就是「成功」。 */
int nim_client_login(void) { return 0; }

int nim_client_relogin(void) { return 0; }
