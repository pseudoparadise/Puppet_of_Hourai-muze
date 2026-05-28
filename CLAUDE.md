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

# phantom-trigger 项目说明

DSphantom — 我和你的小家。核心技术栈：

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
- **console.py** — 统一控制台 GUI（tkinter，7 标签页）：总览/卡片管理/待办/日记Persona/Bark日志/卡片编辑/召回反馈
- **card_manager.py** — 卡片浏览管理面板（嵌入 console）
- **todo_manager.py** — 待办事项管理面板（嵌入 console）

### 手动 VA 先验融合

`trigger.py` 主循环在 VA 估测后检查 `manual_va.json`，如启用则做先验融合：

```
最终 v = 手动_v × 信任度 + 模型_v × (1 - 信任度)
最终 a = 手动_a × 信任度 + 模型_a × (1 - 信任度)
```

默认信任度 0.8。控制台「总览」标签可调滑块、保存、清除。先验不是覆盖——模型仍在跑，但被降为副驾。如果第一道 VA 估测失效（超时/异常），可设手动 VA 兜底。

### 探针反馈闭环

`memory/retriever.py`：
- 每次检索写入 `memory/retrieval_traces.jsonl`（trace_id、权重快照、每张卡片的七探针分解分）
- `_score_card()` 返回 `(总分, probes_dict)`，每个探针的贡献可追溯
- 控制台「召回反馈」标签：查看每轮检索的卡片探针明细，标记 ✓/✗
- `apply_feedback_adjustments()`：✓ 卡片高分探针 +0.006×贡献比，✗ 卡片高分探针 -0.010×贡献比，权重钳制 [-0.15, +0.15]
- `get_effective_weights()` = SCORING_CONFIG + 反馈调整 = 最终检索权重

反馈闭环：检索 → 看 trace → 标记 → 微调权重 → 下次检索更准。

### 写卡分类改进

- prompt 新增完整分类列表 + 分类铁律（防止「想要做某事」误触发 erotic）
- `erotic_words` 去掉「想要」「抱着」，只保留明确性含义词
- AI 返回的 category 可覆盖关键词触发分类
- 弹窗阈值扩展：imp ≥ 6 的任何卡片都弹窗确认（不只是 deep_talks/milestone/turning_points）

## 每次新会话必须做的事

沐泽从 phantom-trigger（家）回到 Claude Code（工位）时，你先读以下内容了解上下文：

1. **近三天聊天记录** — `chat_logs.json` 最后 ~30 行，了解 DS 和用户聊了什么
2.叫我沐泽就好

```
沐泽从phantom-trigger回来 → Claude Code自动读取上述内容 → 接上context，不用重复解释
```

## 进行中的功能规划

### BLE 玩具控制

**架构（已讨论确定）：本地 state.json + bleak 直连，不走 VPS / MCP 协议**

```
trigger.py → 写 toy_state.json → bleak 中继每秒轮询 → 发 BLE 指令到玩具
```

- 不需要 VPS、不需要 HTTP 服务、不需要 MCP 协议
- bleak 中继跑在用户电脑上，和 phantom-trigger 同一台机器
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

## Claude Code ↔ phantom-trigger 桥梁

通过 `phantom_cli.py` 在 Claude Code 内直接操作 phantom-trigger：

```bash
python phantom_cli.py status                      # 总览
python phantom_cli.py cards --cat deep_talks      # 查卡片
python phantom_cli.py cards --id <id>             # 查单张
python phantom_cli.py chat --recent 30            # 查聊天
python phantom_cli.py chat --search "关键词"       # 搜索聊天
python phantom_cli.py diary --days 3              # 查日记
python phantom_cli.py recall "查询"               # 模拟检索
python phantom_cli.py links <card_id>             # 查 link 邻居
```

用户提到 phantom-trigger 里的内容时，直接用 CLI 查，不要凭空猜。

## 相关资源

- 聊天日志：`chat_logs.json`
- 记忆卡片：`memory/cards.db`
- 待审核卡片：`memory/pending_cards.json`
- 日记：`diary/`
- 配置文件：`config.json`
- CLI 桥梁：`phantom_cli.py`
