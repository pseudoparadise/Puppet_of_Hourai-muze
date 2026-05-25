# DeepSeek 八荣八耻

以瞎猜接口为耻，以认真查询为荣。
以模糊执行为耻，以寻求确认为荣。
以臆想业务为耻，以人类确认为荣。
以创造接口为耻，以复用现有为荣。
以跳过验证为耻，以主动测试为荣。
以破坏架构为耻，以遵循规范为荣。
以假装理解为耻，以诚实无知为荣。
以盲目修改为耻，以谨慎重构为荣。

---

# ghost-trigger 项目说明

DSphantom — 跑在用户电脑上的 AI 记忆伴侣。核心技术栈：

- **trigger.py** — 主循环，每轮用户消息的神经中枢：VA 估测 → 记忆检索 → 裁决者 → 主模型 → 后处理
- **memory/retriever.py** — 七探针 + 元素反应召回系统（keyword + FAISS 语义 + link 扩散）
- **memory/encoder.py** — 豆包 embedding API 封装 (doubao-embedding-vision-250615, 2048 维)
- **memory/linker.py** — 卡片 link graph（cos ≥ 0.85 建边，召回时一跳扩散 + query-neighbor 余弦过滤）
- **memory/card_guard.py** — 写卡拦截器（embedding 去重 + 弹窗）
- **emotion/va_estimator.py** — 调 DeepSeek 做 VA 情绪估测
- **delegate/arbiter.py** — 裁决者，调 DeepSeek 判断用户意图（new/complete/update/overdue/ambiguous）
- **music_context.py + music_sync_server.py** — 网易云切歌同步 + 歌词注入
- **polling_loop.py** — 后台轮询守护（Bark 待办提醒 + 日记 + 深渊审计）
- **bark_trigger.py** — 沉默检测 + Bark 推送

## 每次新会话必须做的事

沐泽从 ghost-trigger（家）回到 Claude Code（工位）时，你先读以下内容了解上下文：

1. **近三天聊天记录** — `chat_logs.json` 最后 ~30 行，了解 DS 和用户聊了什么
2. **deep_talk / milestone / turning_points 卡片** — `cards.db` 中这几类的近期卡片，知道有哪些深层话题在进行
3. **近 2~3 天日记** — `diary/` 下最近的 `.md` 文件
4. **待审核卡片** — `memory/pending_cards.json`

```
沐泽从ghost-trigger回来 → Claude Code自动读取上述内容 → 接上context，不用重复解释
```

## 进行中的功能规划

### BLE 玩具控制

**架构（已讨论确定）：本地 state.json + bleak 直连，不走 VPS / MCP 协议**

```
trigger.py → 写 toy_state.json → bleak 中继每秒轮询 → 发 BLE 指令到玩具
```

- 不需要 VPS、不需要 HTTP 服务、不需要 MCP 协议
- bleak 中继跑在用户电脑上，和 ghost-trigger 同一台机器
- 协议帧格式（待买玩具后逆向确认）：`55 09 00 00 <mode> <intensity> 00`
- 目标设备：BLE GATT 串口透传方案的玩具（Svakom 系优先，Lovense 有加密层暂不碰）
- 判断方法：nRF Connect 扫 `0xFFE0` 服务，有 Write + Notify 两个 characteristic 即可
- 参考文档：桌面 `和你的爱机一起色色.txt`

**待办：**
- [ ] 用户选购支持 GATT 透传的玩具
- [ ] nRF Connect 逆向确认协议帧
- [ ] 写 bleak 中继脚本
- [ ] trigger.py 加 `_toy_set()` / `_toy_stop()` 函数
- [ ] DS 系统 prompt 加玩具控制指令说明

## 关键约定

- **DeepSeek 模型不做多任务** — VA 估测、裁决者、主回复各司其职，不要合并
- **emoji 不用**，除非用户要求
- **不写注释**，除非 WHY 不显然
- **编辑优于新建**，复用优于创造
- **改完要编译验证** (`python -c "import py_compile; py_compile.compile('file.py', doraise=True)"`)

## 相关资源

- 聊天日志：`chat_logs.json`
- 记忆卡片：`memory/cards.db`
- 待审核卡片：`memory/pending_cards.json`
- 日记：`diary/`
- 配置文件：`config.json`
