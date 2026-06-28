# 360 门铃 CLI 接管 — 作战蓝图

**目标**: `python doorbell_cli.py stream` → 终端直接看门铃实时视频

---

## ▶ 当前状态 (6/28 晚间)

**A路 UDP P2P**: 全链路通，视频音频已收，flag=0 明文。15 个 ChaCha20 key 已捕获，等待 flag=0x1000000 的加密 session。
**ChaCha20 keys**: 云端下发 15 个 32B ASCII key，索引 30-44，12h 轮换(43200s)。key 是字符串不是 hex 派生——不需要 PBKDF2。
**Frida 可用**: 五劫持抗 libjiagu 验证通过，`auto_keyhunt.py` 一键抓 key。
**D 路不是花椒**: DNS 境外不通。type=0x0106 是电话信令走 P2P relay，不是 FLV CDN。
**0x0230/0x0231 已分析**: 0x0230 inner JSON RC4 解密 ✅ (algo=1, play_type=1)。0x0231 是纯二进制 ACK (104B) ❌不含加密开关。
**Ghidra native 分析**: ChaCha20XOR 由 relay_client::DoProcessPacket 调用，触发条件 flag==0x1000000。key 从父对象 +0x100 复制 (云端下发)。
**RC4 信令解密 (Frida)**: 抓到 base_capacity_res → ea=1 (encryption algorithm=RC4)。req_relay_res → ea=1, ec=0 (encryption cipher=0=ChaCha20不启用), flag=3。**加密开关完整链路**: 云控 JSON → base_capacity (ea=1) → relay 响应 (ea=1,ec=0) → UDP flag=0 → 视频明文。ec=0 就是 flag=0 的原因！

---

## 四条路 (6/28 更新)

```
A路 UDP P2P (主力):  🟡 全通但flag=0→全明文, 等加密session
  ├─ 0x8009信令握手 ✅ (0x0201→0210→0203→0211→0204→0220→0301→0230→0231)
  ├─ 0x0230 netsdk推流 ✅ inner JSON RC4解密 (algo=1, play_type=1)
  ├─ 0x0231 relay回复 ✅ 纯二进制ACK 104B (ret_code=10, 无加密开关)
  ├─ 0x0410喷洒 ✅  0x0403敲门 ✅  4步wrapper协商 ✅
  ├─ 20141104 HELLO+CODEC+AUTH ✅  283B CODEC NEGO ✅
  ├─ 视频流 + 音频流 + 电话信令, 三种数据走同一UDP端口
  │   ├─ wrapper=f594: 0x0002(I帧14帧) + 0x0003(P/B帧410帧) + 0x000a(AAC音频525帧@18fps)
  │   └─ wrapper=6f0e: 0x0106(电话信令323帧, 99-132B, 34s后才出现)
  ├─ 15个32B ChaCha20 key已抓 (索引30-44, 12h轮换), 存captures/chacha20_keys.json
  └─ 阻塞: flag=0=ec=0→ChaCha20XOR从未调用 → 全部明文。根因: relay 响应 ec=0(encryption cipher off)。等 ec≠0 的 session

B路 TCP 信令relay:  ❌ 已死
B2路 TCP cloud (护盘): 🟡 TLS加密, 比UDP先开, 同一视频流
  ├─ 111.206.126.4:443 主连接 (~200kB, 2.07s进场, 比UDP早)
  ├─ 106.63.25.43:443 云中继 (PLAN旧称B2路, 388-404帧)
  ├─ 多个101/106/113网段443并发 (信令relay池的TLS endpoints)
  └─ 待: Frida hook Java Conscrypt 抓TLS明文

C路 HTTP API:  ✅ self_sign.py 全线贯通
  ├─ getRelaySign / rtc2 / codec / g-iot / 云控
  └─ 43.141.130.88 = api.deepseek.com (不是花椒!)

D路 ???:  ❌ 之前猜的花椒FLV CDN不存在
  └─ pl1.live.huajiao.com DNS不通, 0x0106是UDP电话信令不是CDN
```

**两腿协同 (TCP+UDP同一条流)**:
```
t=2.07s  TCP cloud 111.206.126.4:443 进场 (护盘先开)
t=2.93s  UDP CODEC协商开始
t=5.63s  UDP 音频开始
t=7.10s  UDP 视频开始
t=34.1s  电话信令 0x0106 开始 (wrapper=6f0e)
```

---

## 凭据速查

| 项目 | 值 | 来源 |
|------|-----|------|
| SECRET_KEY | `116c46e0b026742bf177b200d31670c3` | 堆dump |
| PK | `d05f4b6e77923f65` | APP硬编码 |
| SN | `86KMR5P06171002806` | 门铃机身 |
| MID (设备固定) | `27da82df-c09e-4990-8466-53cc9a95c10f` | 跨session验证 |
| MID (API/rtc2) | 动态 UUID v4 | Frida |
| Boot RC4 key | `a0e^63b2de5eea&a1451@e0c27c54a80` | Frida Java hook |
| Session RC4 key | `Uf4M9%vxTd,JapdtFM~a*3.cJ~zruX^#` | Frida (每session不同) |
| sname | `db0b5612e0e7` | getRelaySign |
| 屏幕 | 900×1600, 左上原点 | UI dump |
| PLAY | **(450, 660)** | Procreate |
| CONTINUE | **(450, 750)** | Procreate |
| ADB | `127.0.0.1:7555` | MuMu |

**信令 relay (6个, port 80, 永久不变)**:
```
106.39.219.204  106.39.219.205  221.130.199.132
221.130.199.193  42.236.20.205   42.236.20.217
```

---

## ChaCha20 密钥体系 (6/28 确认)

**15个云端下发 key** (Frida hook `QHVCNetGodSees.updateGodSeesVideoStreamSecurityKeys`):
- 索引 30-44, 每个 32B ASCII 字符串 (如 `b!AJzPY%nA5k%LPd@P%nDbhqQmYi27~t`)
- count=43200 (12h轮换), SN=86KMR5P06171002806
- 调用时机: APP init (~9s) + 点 PLAY 后
- 转为 hex: 如 key[30] = `6221414a7a5059256e41356b254c50644050256e44626871516d596932377e74`

**getsk/getak** (native Stats 类):
- `getsk()` → base64 decode → `064cdd04ecaa5e8ed0ed55eb582331b9` (16B)
- `getak()` → base64 decode → `c977ad58239d562e5b4e6d3b161e2ab5` (16B)
- 拼接=32B, 用于 API 签名/upload token, **不是视频流密钥**
- 视频流密钥=云端下发的 15 个, getsk/getak 是身份密钥(两层分离)

**ChaCha20XOR** @ `libtranscore.so + 0x2a5aa0`:
- 触发条件: `puVar8[3] == 0x1000000` (flag 字段)
- 当前所有 capture flag=0 → 从未调用 → 视频是明文
- 包结构: counter(4B)+nonce(12B)+metadata(28B)=44B header, encrypted@offset 44

---

## 0x20141104 消息类型 (6/28 确认)

| type | 内容 | 帧大小 | wrapper前缀 | 帧频 |
|------|------|--------|-------------|------|
| `0x0001` | HELLO 握手 | 48B | 可变 | 握手期 |
| `0x0002` | 视频 I 帧 (SPS/PPS/IDR) | 1032B | f594 | ~14帧/session |
| `0x0003` | 视频 P/B 帧 + SEI | 388-1032B | f594 | ~410帧/session |
| `0x0005` | AUTH 认证 | 40B | 可变 | 握手期 |
| `0x000a` | **AAC 音频** | 109-1032B | f594 | ~18fps (~53ms间隔) |
| `0x0106` | **电话信令** | 99-132B | **6f0e** | 323帧/session |
| `0x012c` | CODEC 协商 | 48-305B | 可变 | 握手期 |

**关键**: f594 和 6f0e 两套 wrapper 复用同一 UDP 端口 (19194), 通过 wrapper 前缀区分通道。

---

## TCP vs UDP 时序

```
t=2.07s  TCP云中继 111.206.126.4:443 进场 (200kB, 护盘先开)
t=2.93s  UDP CODEC 协商开始
t=5.63s  UDP 音频流开始 (0x000a)
t=7.10s  UDP 视频流开始 (0x0002/0x0003)
t=34.1s  UDP 电话信令开始 (0x0106, wrapper=6f0e)
```

**TCP 和 UDP 是同一条流的两条腿** — TCP 先开护盘, UDP P2P 接手主力。单腿必瘸。

---

## Frida 操作 SOP

**五劫持抗 libjiagu** (已验证, 在 `frida_keyhunt_v2.js`):
1. mmap(0x0) 预占 → 2. ptrace 劫持 → 3. exit/_exit/abort 劫持 → 4. kill/raise/tkill/tgkill 劫持 → 5. mprotect 强制 R+X
- Attach 模式安全 (spawn 必死)
- 双脚本 `-l a.js -l b.js` 必 timeout → 合并单脚本
- 脚本里 Java.perform 用 setTimeout 延迟 (等 VM 初始化)

**auto_keyhunt.py 正确时序**:
```
冷启动 → PID出现后等4s → attach Frida → 等hook就位 → 点PLAY → 晃门铃
```
错误时序: 先点PLAY再挂Frida (key 已下发, ChaCha20 已错过)

**关键 hook 点**:
- Java: `com.qihoo.videocloud.api.QHVCNetGodSees.updateGodSeesVideoStreamSecurityKeys`
- Native: `libtranscore.so + 0x2a5aa0` (ChaCha20XOR)

---

## 武器库 (6/28 整理 — 死脚本已移 archive/)

### 协议层 (5个核心)
| 文件 | 功能 |
|------|------|
| `self_sign.py` | MD5 urlSign + HTTP API (getRelaySign/codec/rtc2) |
| `relay_v2.py` | 0x8009 8阶段信令握手 + FRESH模板 |
| `impersonate_v1.py` | 0x20141104 全套builder (HELLO/AUTH/CODEC/ACK/keepalive) |
| `rc4_decrypt.py` | 纯Python RC4解密 |
| `stock_picker.py` | tshark 10s → UDP流量排名 → 找真relay |

### 接管与抓流 (4个)
| 文件 | 功能 |
|------|------|
| `full_chain_host.py` | 宿主机直连全链路 (Phase1-6) |
| `silicon_AO3_relay_hijack_v2.py` | 被动骑劫: APP直播 + tshark → H.264 |
| `hijack_v3.py` | Spy→杀APP→nc重放ACK接管session |
| `doorbell_handshake.py` | Python控制Go client做交互式握手 |

### Frida (9个 — 少而精)
| 文件 | 功能 |
|------|------|
| `frida_keyhunt_v2.js` | **主力**: 五劫持 + key捕获 + ChaCha20XOR hook |
| `frida_spy.js` | **全功能**: 五劫持 + ChaCha20 + AES + RC4 + IO探针 |
| `frida_lite.js` | **轻量**: RC4 + UDP + ChaCha20 (无抗libjiagu) |
| `frida_rc4_hook.js` | Java RC4.decry_RC4 抓密钥 |
| `frida_merged.js` | SSL明文 + URL构造 + MD5输入 三合一 |
| `frida_giot.js` | g-iot 五路并行 (SSL_write_ex + SSL_write + SSL_read + Conscrypt + URL) |
| `frida_ssl_hook.js` | 原生 SSL_read hook (不触发, 参考用) |
| `frida_sherlock.js` | 观察模式: 记录libjiagu杀法不防御 |
| `dlopen_watch.js` | dlopen拦截 → hook http_out.initialize |

### 自动化与辅助 (4个)
| 文件 | 功能 |
|------|------|
| `auto_keyhunt.py` | 一键: 冷启动→挂Frida→点PLAY→抓key+ChaCha20 |
| `capture_fresh.py` | 从模拟器抓APP新鲜握手模板 → fresh_templates.json |
| `app_control.py` | ADB基础库: tap/swipe/screenshot/force-stop |
| `enter_live.py` / `keep_alive.py` | UI Automator 进直播 / CONTINUE 保活 |

### 关键数据文件 (captures/)
| 文件 | 说明 |
|------|------|
| `chacha20_keys.json` | 15个32B ChaCha20 key (raw+hex, 2026-06-28捕获) |
| `fresh_templates.json` | 10种msg_type模板 (5天过期) |
| `blitz_result.json` | ⚠️ 6→6蜜罐映射 (不能用!) |
| `call_video.264` | 铁证: UDP视频可解码 |
| `stock_pick.json` | stock_picker 输出 |

### Go 工具 (tool/ — aarch64交叉编译到模拟器)
| 文件 | 功能 |
|------|------|
| `handshake_client.go` / `handshake_client_arm64` | UDP握手I/O: stdin JSON→stdout JSON |
| `udp_relay.go` / `udp_relay_arm64` | 双向UDP MITM relay |
| `takeover.go` / `takeover_arm64` | Session接管: 循环keepalive+收视频 |

---

## 已验证结论 (禁止重新猜测)

| # | 结论 |
|----|------|
| 1 | 信令 relay 6个 port 80 永久不变 |
| 2 | 8阶段 0x8009 握手: 0201→0210→0203→0211→0204→0220→0301→0231 |
| 3 | 信令加密=RC4 (Java), 视频加密=ChaCha20 (native libtranscore.so) |
| 4 | flag=0 → ChaCha20XOR 不触发 → 视频明文 |
| 5 | 0x0410 喷5个候选, 只1个回 0x0411 → stock_picker.py 找真 relay |
| 6 | blitz_result.json 6→6 静态映射 = 蜜罐 |
| 7 | 视频 relay IP 每 session 不同, 端口动态 |
| 8 | HELLO+CODEC 必须同 tick 连续 sendto, 不等 HELLO_RESP |
| 9 | app_wrapper = byte-swap(relay_step2.bytes[26:27]) |
| 10 | ACK=16B pair, 不是 10B |
| 11 | AUTH_0005 cksum=0xE2F8-wrapper, body=26B, TLVs=0x0003+0x0004 |
| 12 | 283B CODEC NEGO cksum=0xE2FA-seq-wrapper |
| 13 | MID 设备固定值 (27da82df...), 不是随机 UUID |
| 14 | 宿主机直连信令 relay ✅, 宿主机直连视频 relay ❌ (IP 级过滤) |
| 15 | 门铃 motion-triggered — 不晃不推流 |
| 16 | inner_len=2B(H), 不是 4B(I) |
| 17 | magic8 前4B 随机 + 1950de36 后缀, 客户端生成放 0x0220 请求 |
| 18 | Phase 5 后需向信令 relay 发 0x0230 netsdk 推流激活, 不是 0x0220 |
| 19 | key 是云端下发 15 个 32B ASCII, 不是 getsk/getak PBKDF2 派生 |
| 20 | getsk/getak = 身份密钥(API签名), 15 keys = 会话密钥(视频流) |
| 21 | TCP 2.07s 先进场护盘, UDP 后接手主力 — 同一条流的两条腿 |
| 22 | 4种UDP类型: 0x0002(I帧) 0x0003(P/B帧) 0x000a(AAC音频) 0x0106(电话信令) |
| 23 | wrapper f594=视频音频, 6f0e=电话信令, 同端口复用 |
| 24 | 43.141.130.88 = api.deepseek.com, 不是花椒 CDN |
| 25 | 0x0231 relay回复是纯二进制 ACK (104B)，结构: flags+prefix+PK/SN+MID+ts+ret_code+flag_byte，不含 JSON/data 字段，不含加密开关 |
| 26 | ChaCha20XOR 由 relay 服务端 flag 位控制 ( → 设 secure mode,  → 执行解密)。APP 不主动决定加密，等 relay 给 flag |
| 27 | RC4 解密 relay 响应: base_capacity_res → ea=1 (信令RC4)。req_relay_res → ea=1, ec=0, flag=3。**ec=0 就是视频 flag=0 的根因**。ec 由 relay 下发，受云控 JSON 控制 |

---

## Agent 五诫

1. **别新建 .py** — import self_sign / impersonate_v1 / relay_v2 / stock_picker / rc4_decrypt
2. **别硬算密钥** — Frida hook 5 分钟抓到, MD5 sign 公式已破
3. **Frida attach 别 spawn** — libjiagu spawn 必死, attach 安全
4. **先挂 Frida 再点 PLAY** — 时序反了 key 和 ChaCha20 都错过
5. **ADB 是 127.0.0.1:7555** — 不是 5559

## Agent 最容易犯的错

1. 混淆 0x0220(健康检查) 和 0x0230(直播调度)
2. RC4 和 ChaCha20 用错层 — 信令=RC4, 视频=ChaCha20
3. 以为 flag 非零 → 实际当前 session 全明文
4. 对 flag=0 的包跑 ChaCha20 解密
5. 等 HELLO_RESP 再发 CODEC → 真 APP 同 tick 连续 blast
6. 换源端口 → relay 用源端口追踪 session
7. 宿主机直连视频 relay → 全部 timeout (IP 级过滤)
8. byte-swap 算错 wrapper → app_wrapper = byte-swap(relay 提议)
9. inner_len 用 4B(I) 不用 2B(H)

---

## 下一步 (6/28 晚间)

1. **触发 ec≠0** — 根因确认: relay 响应 ec=0 → flag=0 → 无 ChaCha20。需搞清楚什么条件让 relay 设 ec=1 (云控JSON字段? 设备白名单? 固件版本?) 。最直接: Frida hook Stats.decrypt 抓 5208B 云控 JSON, 搜 encrypt/crypto/security 字段
2. **云控/baseCapacity 溯源** — baseCapacity 不在 native so，在 classes4.dex (Java)。用 jadx 搜 `onDeviceBaseCapacityCallBack` / `encryptAlgoCache` 看 algo 怎么从云控 JSON 流入信令加密
3. **B2路** — Frida hook Java Conscrypt → TCP cloud TLS 明文
4. **0x0106 电话信令** — 深挖 6f0e wrapper 下的协议结构
5. **C 路 API 封装** — self_sign.py 已有, 集成到统一 CLI

---

## 关键文件索引

- `captures/`: fresh_templates.json, blitz_result.json(⚠️蜜罐), call_video.264
- `relay_token.json`: getRelaySign 缓存 (5天有效)
- `memory/`: 相关技术文档 (360-crypto-keys.md 等)
- `tool/`: frida-server, jadx, jre, libtranscore.so, classes4.dex
