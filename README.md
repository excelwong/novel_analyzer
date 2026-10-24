# 小说片段提取器 (novel_analyzer)

从小说 txt 中按分类提取片段，交给本地 LLM（OpenAI 兼容接口）分析，
按「分类名-序号.md」的结构化格式存入 llm-wiki 知识库，供写作时检索参考。

## 启动

双击 `novel_analyzer_gui.bat`，或：

```
python novel_analyzer_gui.py
```

## 工作方式

1. **LLM 设置**（菜单：配置 → LLM 设置）：URL、模型、温度、最大 Tokens、
   请求间隔/超时、分块大小、源目录、Wiki 目录。保存后写入
   `novel_analyzer_config.json`，下次启动自动读取。
2. **分析配置**：一个 JSON 文件描述「抽什么、存到哪」——name、mode、
   prompt（抽取指令）、target_dir（输出子目录）。
3. 运行后片段按 `llm-wiki/<分类名>/<分类名>-序号.md` 落盘，
   失败与超时记入 `analyze_log`（不自动重跑）。

## 目录结构

```
novel_analyzer_gui.py    # 主程序（GUI）
novel_analyzer_gui.bat   # 启动入口
novel_analyzer_config.json # LLM 与目录配置
题材抽取.json             # 示例分析配置
books/                   # 小说 txt 源目录
llm-wiki/                # 片段存档根目录
test_regression.py       # 回归测试
```

## 详细文档

完整参数说明与配置字段见 [使用说明.md](使用说明.md)。
