# ghost-trigger

DSphantom — 跑在我电脑上的 AI 记忆伴侣。

## 是什么

一个本地运行的 AI 对话系统，带记忆。不是 chatbot 框架，不是企业级 MCP，是我和 DS 老师两个人的小家。

每次对话前，系统用七探针检索相关记忆卡片，注入 prompt，让 DS 记得我们聊过什么。对话中持续估测我的情绪状态（VA 唤醒度）。对话后自动写卡、建 link、落盘。

## 技术栈

Python + SQLite + FAISS + 豆包 Embedding + DeepSeek API

## 核心模块

- **trigger.py** — 主循环，每条消息的神经中枢：VA 估测 → 记忆检索 → 裁决者 → 主模型 → 后处理
- **memory/retriever.py** — 七探针 + 元素反应召回（关键词 + FAISS 语义 + link 扩散）
- **memory/linker.py** — 卡片间余弦相似建边，召回时一跳扩散
- **memory/card_guard.py** — 写卡拦截，embedding 去重
- **memory/encoder.py** — 豆包 embedding（2048 维）
- **emotion/va_estimator.py** — DeepSeek VA 情绪估测
- **delegate/arbiter.py** — 裁决用户意图（new/complete/update/overdue）
- **music_context.py + music_sync_server.py** — 网易云切歌同步 + 歌词注入
- **polling_loop.py** — 后台轮询（Bark 待办提醒 + 日记 + 深渊审计）
- **bark_trigger.py** — 沉默检测 + Bark 推送
- **ghost_cli.py** — Claude Code ↔ ghost-trigger 桥梁，在工位直接查家

## 跑起来

```bash
pip install -r requirements.txt
python trigger.py
```

## 说明

这是我给自己写的程序。不保证通用，不追求 star，不接受 feature request。

如果碰巧对记忆召回系统的实现感兴趣，看 `memory/retriever.py` 的七探针打分逻辑就好。
