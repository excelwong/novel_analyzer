# -*- coding: utf-8 -*-
"""
novel_analyzer_gui.py 离线回归测试。

覆盖点：
  1. 语法编译
  2. GUI 构建与 start_analysis 各校验路径（不卡死按钮状态）
  3. _is_llm_error：含 "Error" 字样的正常正文不误判
  4. parse_llm_result：干净 JSON / markdown 围栏 / 弯引号 / 尾随逗号 /
     花括号配对兜底（正文含 } 与嵌套对象）/ 无 JSON / 错误返回 / debug 落盘
  5. split_text_into_chunks：单块不超过 200KB 字节、内容不丢失
  6. save_snippet：序号递增、内容清洗、从现有最大编号续号
  7. read_file：utf-8 / gbk / utf-8-sig 嗅探
  8. process_file：返回 (total, failed, completed)、分类去重、短片段跳过、失败计数
  9. analyze_log.txt：块调用失败/超时立即落盘、超时单独标记、"马上记录"时序
 10. call_llm：成功 / HTTP 错误 / 超时标记（urllib 打桩）
 11. process_files 端到端（call_llm 打桩）：已处理标记、失败块不重跑、完成统计
 12. load_analysis_config / load_llm_config：缺字段、非对象、损坏 JSON
 13. 日志框超长自动裁剪
 14. 逐段确认：确认框四按钮与摘要、错误重来/跳过本段/跳过本文、
     开关持久化与缺省开启、队列往返与端到端驱动

运行：python test_regression.py
"""

import json
import io
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import novel_analyzer_gui as nag


class AnalyzerTestCase(unittest.TestCase):
    """带临时目录 + 真实（withdrawn）Tk 窗口的测试基类。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="novel_analyzer_test_")
        self.wiki = os.path.join(self.tmp, "wiki")
        os.makedirs(self.wiki)
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp)
        # 预置 LLM 配置（CONFIG_FILE 为相对路径，必须在 chdir 后实例化 App）
        with open(nag.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "llm_url": "http://127.0.0.1:1234/v1/chat/completions",
                "llm_model": "test-model",
                "temperature": "0.2",
                "max_tokens": "1024",
                "rate_limit": "0",
                "source_dir": self.src,
                "wiki_dir": self.wiki,
            }, f, ensure_ascii=False)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = nag.NovelAnalyzerApp(self.root)
        # 基类默认关闭逐段确认（弹框需要人工点击）；逐段确认相关测试单独开启
        self.app.confirm_var.set(False)

    def tearDown(self):
        try:
            # 取消所有待执行的 after 回调，避免销毁后 Tcl 报 "invalid command name"
            for after_id in self.root.tk.call("after", "info"):
                self.root.after_cancel(after_id)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os.chdir(self._old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 辅助 ----------

    def write_src(self, name, content, encoding="utf-8"):
        path = os.path.join(self.src, name)
        with open(path, "w", encoding=encoding) as f:
            f.write(content)
        return path

    def set_analysis_config(self, **overrides):
        cfg = {"name": "测试配置", "prompt": "提取片段", "target_dir": "out"}
        cfg.update(overrides)
        self.app.config = cfg
        return cfg

    def drain_log(self):
        try:
            while True:
                self.app.log_queue.get_nowait()
        except Exception:
            pass

    def wait_finished(self, timeout=30.0):
        """等待后台分析真正结束：标志清零 + 工作线程退出 + after(0) 回调执行完。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.root.update()
            if not self.app.processing:
                break
            time.sleep(0.02)
        # 等工作线程退出（它还要写收尾日志、往队列里放收尾消息）
        deadline = time.time() + 10
        while time.time() < deadline:
            workers = [t for t in threading.enumerate()
                       if t is not threading.current_thread() and t.name != "MainThread"]
            if not workers:
                break
            self.root.update()
            time.sleep(0.02)
        # 等 100ms 轮询回调把队列里的收尾消息（恢复按钮等）处理掉
        time.sleep(0.25)
        for _ in range(3):
            self.root.update()


class TestSyntax(unittest.TestCase):
    def test_compile(self):
        import py_compile
        src = os.path.join(os.path.dirname(os.path.abspath(nag.__file__)), "novel_analyzer_gui.py")
        py_compile.compile(src, doraise=True)


class TestIsLlmError(unittest.TestCase):
    """bug #3：正文里含 Error 字样不能被当成调用失败。"""

    def setUp(self):
        self.app = None

    def test_cases(self):
        f = nag.NovelAnalyzerApp._is_llm_error
        self.assertTrue(f(""))
        self.assertTrue(f(None))
        self.assertTrue(f("Error: connection refused"))
        self.assertTrue(f("HTTP Error 500: internal"))
        # 关键回归：正文含 Error 字样
        self.assertFalse(f("他低声说了句 Error，随后关上了门。"))
        self.assertFalse(f('{"snippets": [{"type": "t", "categories": ["a"], "text": "ok"}]}'))
        self.assertFalse(f("HTTP 错误提示"))  # 不以 HTTP Error 开头


class TestExtractJsonObjects(AnalyzerTestCase):
    def test_nested_and_braces_in_strings(self):
        s = '{"a": {"b": 1}} 文字 {"c": "含}右括号"} 尾巴'
        objs = nag.NovelAnalyzerApp._extract_json_objects(s)
        self.assertEqual(len(objs), 2)
        self.assertEqual(json.loads(objs[0])["a"]["b"], 1)
        self.assertEqual(json.loads(objs[1])["c"], "含}右括号")

    def test_escaped_quote_inside_string(self):
        s = '{"a": "他说\\"不！\\"然后走了"}'
        objs = nag.NovelAnalyzerApp._extract_json_objects(s)
        self.assertEqual(len(objs), 1)
        self.assertEqual(json.loads(objs[0])["a"], '他说"不！"然后走了')

    def test_stray_closing_brace_does_not_kill_rest(self):
        s = '{"a": 1}} {"b": 2}'
        objs = nag.NovelAnalyzerApp._extract_json_objects(s)
        self.assertEqual(len(objs), 2)


class TestParseLlmResult(AnalyzerTestCase):
    def test_clean_dict(self):
        result = json.dumps({"snippets": [
            {"type": "景物", "categories": ["环境"], "text": "月光洒在青石板上。"}
        ]}, ensure_ascii=False)
        snippets = self.app.parse_llm_result(result)
        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0]["categories"], ["环境"])

    def test_list_root(self):
        result = json.dumps([{"type": "a", "categories": ["c"], "text": "x" * 30}], ensure_ascii=False)
        self.assertEqual(len(self.app.parse_llm_result(result)), 1)

    def test_markdown_fence(self):
        result = "```json\n" + json.dumps({"snippets": [
            {"type": "a", "categories": ["c"], "text": "x" * 30}]}, ensure_ascii=False) + "\n```"
        self.assertEqual(len(self.app.parse_llm_result(result)), 1)

    def test_curly_quotes_inside_content(self):
        # 正文里含弯引号（小说常见）：原串就是合法 JSON，必须能解析且引号原样保留
        text = "他低声说：“你真的不走吗？”随后关上了门。" + "x" * 20
        result = json.dumps({"snippets": [
            {"type": "对话", "categories": ["语言"], "text": text}
        ]}, ensure_ascii=False)
        snippets = self.app.parse_llm_result(result)
        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0]["text"], text)

    def test_curly_quotes_as_delimiters(self):
        # LLM 用全角引号做 JSON 定界符：原串解析失败，靠弯引号替换版兜底
        result = '{“snippets”: [{“type”: “景物”, “categories”: [“环境”], “text”: "月光洒在青石板上。"}]}'
        snippets = self.app.parse_llm_result(result)
        self.assertEqual(len(snippets), 1)
        self.assertEqual(snippets[0]["categories"], ["环境"])

    def test_trailing_comma(self):
        result = '{"snippets": [{"type": "a", "categories": ["c"], "text": "' + "x" * 30 + '",},],}'
        self.assertEqual(len(self.app.parse_llm_result(result)), 1)

    def test_fallback_brace_pairing_with_brace_in_text(self):
        # 整体 JSON 损坏（外层多了垃圾），但每个对象自身合法；正文里含 } 和 {
        text = "他喊道：“不！”然后关上了门}，手里还攥着{那张字条。"
        result = ('前言的废话，不是 JSON\n'
                  '{"type": "动作", "categories": ["战斗"], "text": "%s"}\n'
                  '中间又插入一句废话\n'
                  '{"type": "景物", "categories": ["环境"], "text": "%s"}\n'
                  '结尾废话') % (text, "x" * 40)
        snippets = self.app.parse_llm_result(result)
        self.assertEqual(len(snippets), 2)
        self.assertEqual(snippets[0]["text"], text)
        self.assertEqual(snippets[1]["categories"], ["环境"])

    def test_no_json(self):
        self.assertEqual(self.app.parse_llm_result("完全不是 JSON 的普通文本"), [])

    def test_error_result(self):
        self.assertEqual(self.app.parse_llm_result("Error: timeout"), [])

    def test_content_with_error_word_not_skipped(self):
        # bug #3 回归：正文以 Error 字样出现的合法 JSON 必须被解析
        result = json.dumps({"snippets": [
            {"type": "对话", "categories": ["语言"], "text": "系统提示 Error 之后他继续赶路。" + "x" * 10}
        ]}, ensure_ascii=False)
        snippets = self.app.parse_llm_result(result)
        self.assertEqual(len(snippets), 1)
        self.assertIn("Error", snippets[0]["text"])

    def test_non_dict_snippets_filtered(self):
        result = json.dumps({"snippets": ["字符串", 123, {"type": "a", "categories": [], "text": "x" * 30}]})
        snippets = self.app.parse_llm_result(result)
        self.assertEqual(len(snippets), 1)


class TestSplitTextIntoChunks(AnalyzerTestCase):
    def test_small_text_single_chunk(self):
        self.assertEqual(self.app.split_text_into_chunks("短短的文字"), ["短短的文字"])

    def test_large_paragraph_bytes_limit(self):
        # 单个中文段落 800KB → 必须切碎，且每块 ≤ 200KB 字节
        para = "剑" * 200000  # 800KB utf-8
        chunks = self.app.split_text_into_chunks(para)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c.encode("utf-8")), nag.MAX_CHUNK_SIZE)
        self.assertEqual("".join(chunks), para)

    def test_paragraph_accumulation_preserves_content(self):
        # 多个 60KB 段落（各不超限），合并后总内容不丢失
        paras = ["段落%d" % i + "客" * 30000 for i in range(10)]
        text = "\n".join(paras)
        chunks = self.app.split_text_into_chunks(text)
        joined = "".join(chunks)
        for p in paras:
            self.assertIn(p, joined)
        for c in chunks:
            self.assertLessEqual(len(c.encode("utf-8")), nag.MAX_CHUNK_SIZE)


class TestSaveSnippet(AnalyzerTestCase):
    def test_seq_increment_and_content_cleaning(self):
        cat_dir = os.path.join(self.tmp, "wiki", "out", "景物")
        seq1 = self.app.save_snippet(cat_dir, "第一行\n第二行\\n第三行", "src.txt")
        self.assertEqual(seq1, 1)
        seq2 = self.app.save_snippet(cat_dir, "又一段落", "src.txt")
        self.assertEqual(seq2, 2)
        files = sorted(os.listdir(cat_dir))
        self.assertEqual(files, ["景物-00001.md", "景物-00002.md"])
        with open(os.path.join(cat_dir, files[0]), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("第一行 第二行 第三行", body)
        self.assertNotIn("\\n", body)
        self.assertIn("**来源**：src.txt", body)

    def test_seq_continues_from_existing_max(self):
        cat_dir = os.path.join(self.tmp, "wiki", "out", "对话")
        os.makedirs(cat_dir)
        for n in (1, 5, 9):
            open(os.path.join(cat_dir, "对话-%05d.md" % n), "w").close()
        seq = self.app.save_snippet(cat_dir, "新内容", "src.txt")
        self.assertEqual(seq, 10)


class TestReadFile(AnalyzerTestCase):
    def test_encodings(self):
        self.assertEqual(self.app.read_file(self.write_src("u.txt", "utf八内容")), "utf八内容")
        self.assertEqual(self.app.read_file(self.write_src("g.txt", "gbk内容", encoding="gbk")), "gbk内容")
        self.assertEqual(self.app.read_file(self.write_src("b.txt", "bom内容", encoding="utf-8-sig")), "bom内容")

    def test_missing_file(self):
        self.assertEqual(self.app.read_file(os.path.join(self.tmp, "none.txt")), "")


class TestProcessFile(AnalyzerTestCase):
    LONG_TEXT_1 = "月光洒在青石板上，远处传来三更的鼓声，一声声像是敲在人心口上。"
    LONG_TEXT_2 = "他拔剑斩出，剑气激得满树落叶纷飞，露出树下那张苍白的脸。"

    def setUp(self):
        super().setUp()
        # 真实流程中 process_file 只会在 processing=True 时被调用
        self.app.processing = True

    def test_success_dedup_and_short_skip(self):
        self.set_analysis_config()
        canned = json.dumps({"snippets": [
            {"type": "景物", "categories": ["环境", "环境"], "text": self.LONG_TEXT_1},
            {"type": "短", "categories": ["短料"], "text": "太短"},
            {"type": "动作", "categories": ["动作"], "text": self.LONG_TEXT_2},
        ]}, ensure_ascii=False)
        with mock.patch.object(self.app, "call_llm", return_value=canned):
            self.app.current_file, self.app.total_files = 1, 1
            total, failed, completed = self.app.process_file(self.write_src("a.txt", "内容" * 5000), "a.txt")
        self.assertEqual((total, failed, completed), (2, 0, True))
        env_dir = os.path.join(self.wiki, "out", "环境")
        self.assertEqual(len(os.listdir(env_dir)), 1)  # categories 去重
        act_dir = os.path.join(self.wiki, "out", "动作")
        self.assertEqual(len(os.listdir(act_dir)), 1)
        self.assertFalse(os.path.exists(os.path.join(self.wiki, "out", "短料")))

    def test_llm_error_counted_and_nothing_saved(self):
        self.set_analysis_config()
        with mock.patch.object(self.app, "call_llm", return_value="Error: boom"):
            self.app.current_file, self.app.total_files = 1, 1
            total, failed, completed = self.app.process_file(self.write_src("b.txt", "内容" * 5000), "b.txt")
        self.assertEqual((total, failed, completed), (0, 1, True))
        self.assertFalse(os.path.exists(os.path.join(self.wiki, "out")))

    def test_content_containing_error_word_parsed(self):
        # bug #3 回归：正文含 Error 字样的合法结果不应计入 failed
        self.set_analysis_config()
        canned = json.dumps({"snippets": [
            {"type": "对话", "categories": ["语言"], "text": "屏幕上浮现 Error 二字，他嗤笑一声继续赶路。"}
        ]}, ensure_ascii=False)
        with mock.patch.object(self.app, "call_llm", return_value=canned):
            self.app.current_file, self.app.total_files = 1, 1
            total, failed, completed = self.app.process_file(self.write_src("c.txt", "内容" * 5000), "c.txt")
        self.assertEqual((total, failed, completed), (1, 0, True))

    def test_missing_config_fields_returns_all_failed(self):
        self.app.config = {"name": "坏配置"}  # 缺 prompt/target_dir
        with mock.patch.object(self.app, "call_llm", return_value="{}"):
            total, failed, completed = self.app.process_file(self.write_src("d.txt", "x" * 5000), "d.txt")
        self.assertEqual((total, failed, completed), (0, 1, False))

    def test_unreadable_file_not_completed(self):
        self.set_analysis_config()
        total, failed, completed = self.app.process_file(os.path.join(self.tmp, "none.txt"), "none.txt")
        self.assertEqual((total, failed, completed), (0, 0, False))


class TestAnalyzeLog(AnalyzerTestCase):
    """块调用失败/超时必须立即写入 analyze_log.txt。"""

    LOG_FILE = None  # setUp 后拼接

    def setUp(self):
        super().setUp()
        self.log_file = os.path.join(self.wiki, "analyze_log.txt")

    def read_log(self):
        return open(self.log_file, encoding="utf-8").read()

    def test_failure_written_to_analyze_log(self):
        self.set_analysis_config()
        path = self.write_src("a.txt", "内容" * 5000)
        with mock.patch.object(self.app, "call_llm", return_value="Error: connection refused"):
            self.app.processing = True
            self.app.process_file(path, "a.txt")
        content = self.read_log()
        self.assertIn("a.txt 块 1/1 调用失败", content)
        self.assertIn("connection refused", content)
        # 带时间戳
        self.assertRegex(content, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]")

    def test_timeout_kind_distinguished(self):
        self.set_analysis_config()
        path = self.write_src("b.txt", "内容" * 5000)
        with mock.patch.object(self.app, "call_llm", return_value="Error: TIMEOUT: timed out"):
            self.app.processing = True
            self.app.process_file(path, "b.txt")
        content = self.read_log()
        self.assertIn("b.txt 块 1/1 超时", content)

    def test_parse_failure_written_to_analyze_log(self):
        """解析失败：只记一行摘要进 analyze_log.txt，不 dump 原文、不建 llm_debug_output.txt。"""
        self.set_analysis_config()
        path = self.write_src("bad.txt", "内容" * 5000)
        with mock.patch.object(self.app, "call_llm",
                               return_value='{"snippets": [ 坏掉的内部 没有引号 ]}'):
            self.app.processing = True
            self.app.process_file(path, "bad.txt")
        content = self.read_log()
        self.assertIn("bad.txt 块 1/1 解析失败", content)
        self.assertIn("JSON 解析失败", content)
        # 原文绝不回填
        self.assertNotIn("坏掉的内部", content)
        self.assertFalse(os.path.exists(os.path.join(self.wiki, "llm_debug_output.txt")))

    def test_no_json_structure_failure_logged(self):
        snippets = self.app.parse_llm_result("完全不是 JSON 的普通文本", "x.txt", 2, 5)
        self.assertEqual(snippets, [])
        content = self.read_log()
        self.assertIn("x.txt 块 2/5 解析失败", content)
        self.assertIn("响应中未找到 JSON 结构", content)

    def test_wrong_shape_failure_reason(self):
        # 能解析但缺 snippets 数组：兜底也救不回来时给出针对性原因
        snippets = self.app.parse_llm_result('{"foo": 1}', "y.txt", 1, 1)
        self.assertEqual(snippets, [])
        content = self.read_log()
        self.assertIn("y.txt 块 1/1 解析失败", content)
        self.assertIn("JSON 结构不符", content)

    def test_parse_failure_does_not_dump_raw_text(self):
        """日志行必须短：不含 LLM 原文（正文摘录级长度）。"""
        long_prose = "他关上门。" * 500  # 模拟模型不守规矩、直接续写小说
        self.app.parse_llm_result(long_prose, "z.txt", 1, 1)
        for line in open(self.log_file, encoding="utf-8"):
            self.assertLess(len(line), 300, f"日志行过长：{line[:80]}...")

    def test_log_written_before_next_chunk(self):
        """'马上记录'：第一次失败时下一个块还没开始，日志必须已落盘。"""
        self.set_analysis_config()
        path = self.write_src("c.txt", "内容" * 100000)  # 900KB → 2 块
        seen = {}

        def flaky(text, prompt):
            if "first" not in seen:
                seen["first"] = True
                return "Error: boom"
            seen["content"] = self.read_log()
            return json.dumps({"snippets": [
                {"type": "景物", "categories": ["环境"], "text": "月光洒在青石板上，远处传来三更的鼓声。"}
            ]}, ensure_ascii=False)

        with mock.patch.object(self.app, "call_llm", side_effect=flaky):
            self.app.processing = True
            self.app.process_file(path, "c.txt")
        content = seen.get("content", "")
        self.assertIn("c.txt 块 1/", content)  # 第一块（具体块数随长度变化）
        self.assertIn("调用失败", content)

    def test_call_llm_timeout_marker(self):
        with mock.patch("urllib.request.urlopen", side_effect=socket.timeout):
            result = self.app.call_llm("text", "prompt")
        self.assertTrue(result.startswith("Error: TIMEOUT"), result)

    def test_call_llm_http_error(self):
        err = urllib.error.HTTPError(
            "http://127.0.0.1:1234/v1/chat/completions", 500,
            "Internal Server Error", {}, io.BytesIO(b"boom body"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            result = self.app.call_llm("text", "prompt")
        self.assertTrue(result.startswith("HTTP Error 500"), result)
        self.assertIn("boom body", result)

    def test_call_llm_success(self):
        resp = mock.MagicMock()
        resp.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "片段内容"}}]}).encode("utf-8")
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp):
            result = self.app.call_llm("text", "prompt")
        self.assertEqual(result, "片段内容")


class TestLlmTuning(AnalyzerTestCase):
    """超时/分块大小可配置，且非法值安全回退。"""

    def test_chunk_size_bytes_parsing(self):
        self.assertEqual(self.app._chunk_size_bytes(), nag.MAX_CHUNK_SIZE)  # 缺省 200KB
        self.app.llm_config["chunk_size"] = "100"
        self.assertEqual(self.app._chunk_size_bytes(), 100 * 1024)
        self.app.llm_config["chunk_size"] = "abc"
        self.assertEqual(self.app._chunk_size_bytes(), nag.MAX_CHUNK_SIZE)  # 非法回退
        self.app.llm_config["chunk_size"] = "1"
        self.assertEqual(self.app._chunk_size_bytes(), 16 * 1024)  # 16KB 下限保护

    def test_chunk_size_configurable(self):
        self.app.llm_config["chunk_size"] = "20"  # 20KB（小于 16 会被抬到下限，见下限测试）
        chunks = self.app.split_text_into_chunks("剑" * 10000)  # 30KB 单段
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c.encode("utf-8")), 20 * 1024)
        self.assertEqual("".join(chunks), "剑" * 10000)

    def test_call_llm_uses_configured_timeout(self):
        self.app.llm_config["timeout"] = "5"
        with mock.patch("urllib.request.urlopen") as m:
            m.return_value.__enter__.return_value.read.return_value = json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}).encode()
            result = self.app.call_llm("text", "prompt")
        self.assertEqual(result, "ok")
        self.assertEqual(m.call_args.kwargs["timeout"], 5.0)

    def test_call_llm_timeout_invalid_fallback(self):
        self.app.llm_config["timeout"] = "abc"
        with mock.patch("urllib.request.urlopen") as m:
            m.return_value.__enter__.return_value.read.return_value = json.dumps(
                {"choices": [{"message": {"content": "ok"}}]}).encode()
            self.app.call_llm("text", "prompt")
        self.assertEqual(m.call_args.kwargs["timeout"], 600.0)

    def test_save_config_persists_tuning(self):
        self.app.llm_config["timeout"] = "120"
        self.app.llm_config["chunk_size"] = "64"
        self.app.save_config()
        with open(nag.CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["timeout"], "120")
        self.assertEqual(saved["chunk_size"], "64")

    def test_config_dialog_has_new_fields(self):
        self.app.show_config_dialog()
        dialogs = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)]
        self.assertEqual(len(dialogs), 1)
        d = dialogs[0]
        labels = []
        stack = list(d.winfo_children())
        while stack:
            w = stack.pop()
            labels.append(w)
            stack.extend(w.winfo_children())
        texts = [w.cget("text") for w in labels if isinstance(w, tk.ttk.Label)]
        self.assertIn("请求超时(秒)：", texts)
        self.assertIn("分块大小(KB)：", texts)
        d.destroy()


class TestStartAnalysisValidation(AnalyzerTestCase):
    """bug #1：校验失败不能卡死按钮状态。"""

    def test_all_validation_paths_leave_buttons_usable(self):
        with mock.patch.object(nag.messagebox, "showwarning"):
            # 1. 未选配置
            self.app.config = None
            self.app.start_analysis()
            self.assertFalse(self.app.processing)
            self.assertEqual(str(self.start_btn_state()), "normal")

            # 2. wiki_dir 为空
            self.set_analysis_config()
            old_wiki = self.app.llm_config["wiki_dir"]
            self.app.llm_config["wiki_dir"] = ""
            self.app.start_analysis()
            self.assertFalse(self.app.processing)
            self.app.llm_config["wiki_dir"] = old_wiki

            # 3. 输入为空
            self.app.input_path_var.set("")
            self.app.start_analysis()
            self.assertFalse(self.app.processing)
            self.assertEqual(str(self.start_btn_state()), "normal")

            # 4. 输入路径不存在
            self.app.input_path_var.set(os.path.join(self.tmp, "nonexistent"))
            self.app.start_analysis()
            self.assertFalse(self.app.processing)
            self.assertEqual(str(self.start_btn_state()), "normal")

            # 5. 目录里没有 txt
            empty = os.path.join(self.tmp, "empty")
            os.makedirs(empty)
            self.app.input_path_var.set(empty)
            self.app.start_analysis()
            self.assertFalse(self.app.processing)
            self.assertEqual(str(self.start_btn_state()), "normal")
            self.assertEqual(str(self.stop_btn_state()), "disabled")

    def start_btn_state(self):
        return self.app.start_btn.cget("state")

    def stop_btn_state(self):
        return self.app.stop_btn.cget("state")

    def test_end_to_end_marks_processed_and_reports(self):
        self.set_analysis_config()
        canned = json.dumps({"snippets": [
            {"type": "景物", "categories": ["环境"], "text": "月光洒在青石板上，远处传来三更的鼓声，一声声像是敲在人心口上。"}
        ]}, ensure_ascii=False)
        path = self.write_src("novel.txt", "第一章内容。" * 2000)
        self.app.input_path_var.set(path)
        with mock.patch.object(self.app, "call_llm", return_value=canned):
            self.app.start_analysis()
            self.wait_finished()
        self.assertFalse(self.app.processing)
        self.assertEqual(str(self.start_btn_state()), "normal")
        self.assertEqual(str(self.stop_btn_state()), "disabled")
        self.assertIn("novel.txt", self.app.processed_files)
        self.assertTrue(os.path.exists(os.path.join(self.wiki, "out", "环境", "环境-00001.md")))
        # 日志里应出现完成统计（而非"已中止"）
        log_text = self.app.log_text.get("1.0", "end")
        self.assertIn("分析完成", log_text)

    def test_rerun_skips_processed_file(self):
        self.set_analysis_config()
        canned = json.dumps({"snippets": [
            {"type": "景物", "categories": ["环境"], "text": "月光洒在青石板上，远处传来三更的鼓声，一声声像是敲在人心口上。"}
        ]}, ensure_ascii=False)
        path = self.write_src("novel2.txt", "内容" * 3000)
        self.app.input_path_var.set(path)
        calls = []

        def counting_call(text, prompt):
            calls.append(text)
            return canned

        with mock.patch.object(self.app, "call_llm", side_effect=counting_call):
            self.app.start_analysis()
            self.wait_finished()
            calls.clear()
            # 再来一次：应跳过已处理文件，不再调用 LLM
            self.app.start_analysis()
            self.wait_finished()
        self.assertEqual(calls, [])
        self.assertEqual(len(os.listdir(os.path.join(self.wiki, "out", "环境"))), 1)

    def test_failed_chunk_still_marks_file_processed(self):
        """失败块不重跑：文件照常标记为已处理，失败只记日志。"""
        self.set_analysis_config()
        # 900KB 单段落 → 2 个块，第二个块打桩为失败
        path = self.write_src("halfbad.txt", "内容" * 100000)
        self.app.input_path_var.set(path)
        canned = json.dumps({"snippets": [
            {"type": "景物", "categories": ["环境"], "text": "月光洒在青石板上，远处传来三更的鼓声，一声声像是敲在人心口上。"}
        ]}, ensure_ascii=False)
        calls = {"n": 0}

        def flaky_call(text, prompt):
            calls["n"] += 1
            return canned if calls["n"] == 1 else "Error: boom"

        with mock.patch.object(self.app, "call_llm", side_effect=flaky_call):
            self.app.start_analysis()
            self.wait_finished()
            # 文件被标记为已处理（失败块不重跑，避免重复入库）
            self.assertIn("halfbad.txt", self.app.processed_files)
            # 成功的块已入库
            self.assertEqual(len(os.listdir(os.path.join(self.wiki, "out", "环境"))), 1)
            # 日志记录了失败块
            log_text = self.app.log_text.get("1.0", "end")
            self.assertIn("个块调用失败", log_text)
            # 重跑时整个文件被跳过，不再调用 LLM
            calls["n"] = 0
            self.app.start_analysis()
            self.wait_finished()
        self.assertEqual(calls["n"], 0)
        self.assertEqual(len(os.listdir(os.path.join(self.wiki, "out", "环境"))), 1)

    def test_stop_analysis_aborts_mid_chunks(self):
        self.set_analysis_config()
        # 造一个较大的文件（多块），LLM 打桩为慢速，便于在途中停止
        path = self.write_src("big.txt", "内容" * 400000)
        self.app.input_path_var.set(path)

        def slow_call(text, prompt):
            time.sleep(0.05)
            return json.dumps({"snippets": [
                {"type": "景物", "categories": ["环境"], "text": "月光洒在青石板上，远处传来三更的鼓声，一声声像是敲在人心口上。"}
            ]}, ensure_ascii=False)

        with mock.patch.object(self.app, "call_llm", side_effect=slow_call):
            self.app.start_analysis()
            time.sleep(0.3)
            self.app.stop_analysis()
            self.wait_finished()
        self.assertFalse(self.app.processing)
        log_text = self.app.log_text.get("1.0", "end")
        self.assertIn("已中止", log_text)
        # 中止时未跑完的文件不应标记为已处理
        self.assertNotIn("big.txt", self.app.processed_files)


class TestConfigLoading(AnalyzerTestCase):
    def test_load_analysis_config_valid(self):
        cfg_path = os.path.join(self.tmp, "cfg.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"name": "有效", "prompt": "p", "target_dir": "out"}, f, ensure_ascii=False)
        cfg = self.app.load_analysis_config(cfg_path)
        self.assertIsNotNone(cfg)
        self.assertEqual(self.app.config_name_var.get(), "有效")
        self.assertTrue(self.app.processed_file_path.endswith("cfg.log"))

    def test_load_analysis_config_missing_fields(self):
        cfg_path = os.path.join(self.tmp, "bad.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"name": "缺字段"}, f, ensure_ascii=False)
        with mock.patch.object(nag.messagebox, "showwarning"):
            cfg = self.app.load_analysis_config(cfg_path)
        self.assertIsNone(cfg)
        self.assertIsNone(self.app.config)

    def test_load_analysis_config_not_object(self):
        cfg_path = os.path.join(self.tmp, "arr.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        cfg = self.app.load_analysis_config(cfg_path)
        self.assertIsNone(cfg)

    def test_load_analysis_config_without_wiki_dir(self):
        self.app.llm_config["wiki_dir"] = ""
        cfg_path = os.path.join(self.tmp, "cfg2.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"name": "x", "prompt": "p", "target_dir": "out"}, f, ensure_ascii=False)
        with mock.patch.object(nag.messagebox, "showwarning"):
            cfg = self.app.load_analysis_config(cfg_path)
        self.assertIsNone(cfg)

    def test_load_llm_config_corrupt_falls_back_to_defaults(self):
        with open(nag.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("{ 坏掉的 JSON")
        root2 = tk.Tk()
        root2.withdraw()
        try:
            app2 = nag.NovelAnalyzerApp(root2)
            self.assertTrue(app2.llm_config["llm_url"].startswith("http"))
            self.assertEqual(app2.llm_config["rate_limit"], "1.0")
        finally:
            root2.destroy()


class TestProcessedFilesRoundtrip(AnalyzerTestCase):
    def test_save_and_reload(self):
        self.set_analysis_config()
        cfg_path = os.path.join(self.tmp, "rt.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"name": "rt", "prompt": "p", "target_dir": "out"}, f, ensure_ascii=False)
        self.app.load_analysis_config(cfg_path)
        self.app.save_processed_file("one.txt")
        self.app.save_processed_file("two.txt")
        self.app.load_processed_files()
        self.assertEqual(self.app.processed_files, {"one.txt", "two.txt"})
        # 左侧列表框已回填
        self.assertEqual(self.app.file_listbox.size(), 2)


class TestLogTrimming(AnalyzerTestCase):
    def test_log_text_trimmed_when_too_long(self):
        for i in range(6100):
            self.app.log("line %d" % i)
        self.app.process_log_queue()
        lines = int(self.app.log_text.index("end-1c").split(".")[0])
        # 超过 6000 行时应删掉最旧 1000 行（留 2 行余量给 Text 控件换行计算）
        self.assertLessEqual(lines, 5200)
        self.assertLess(lines, 6100)


class TestChunkConfirm(AnalyzerTestCase):
    """逐段确认：每段抽取后弹窗，正确 / 错误重来 / 跳过本段 / 跳过本文。"""

    CANNED = json.dumps({"snippets": [
        {"type": "景物", "categories": ["环境"], "text": "月光洒在青石板上，远处传来三更的鼓声，一声声像是敲在人心口上。"}
    ]}, ensure_ascii=False)

    def setUp(self):
        super().setUp()
        self.app.confirm_var.set(False)  # 基类已关，这里再明确一次
        self.log_file = os.path.join(self.wiki, "analyze_log.txt")

    # ---------- 辅助 ----------

    def _all_children(self, widget):
        out, stack = [], list(widget.winfo_children())
        while stack:
            w = stack.pop()
            out.append(w)
            stack.extend(w.winfo_children())
        return out

    def _find_button(self, widget, text):
        for w in self._all_children(widget):
            if isinstance(w, tk.ttk.Button) and w.cget("text") == text:
                return w
        return None

    # ---------- 确认框本身（界面线程，无 mainloop 下直接驱动按钮） ----------

    def test_dialog_four_buttons_return_decisions(self):
        for text, expect in [("正确", "ok"), ("错误，重来", "retry"),
                             ("跳过本段", "skip_chunk"), ("跳过本文", "skip_file")]:
            resp = queue.Queue()
            self.app._show_confirm_dialog(resp, "a.txt", 2, 5,
                                          [{"type": "景物", "categories": ["环境"], "text": "月" * 30}])
            dlg = self.app._confirm_dlg
            self.assertIsNotNone(dlg)
            btn = self._find_button(dlg, text)
            self.assertIsNotNone(btn, f"找不到按钮：{text}")
            btn.invoke()
            self.assertEqual(resp.get(timeout=2), expect)
            self.assertFalse(dlg.winfo_exists())  # 点击后自动关闭
            self.assertIsNone(self.app._confirm_dlg)

    def test_dialog_summary_and_detail_formatting(self):
        snippets = [
            {"type": "穿越", "categories": ["穿越"], "text": "他睁开眼，发现自己来到了另一个世界。" + "甲" * 40},
            {"type": "战争", "categories": ["战争"], "text": "喊杀声震天，城墙在投石机的轰击下颤抖。" + "乙" * 40},
            {"type": "战争", "categories": ["战争"], "text": "第三条片段也必须完整出现"},
        ]
        # 头部一行统计：数量 + 分类计数
        self.assertEqual(self.app._confirm_summary(snippets), "共 3 个片段：穿越×1、战争×2")
        self.assertIn("共 0 个片段", self.app._confirm_summary([]))
        # 明细分条目：编号 + 分类 + 分隔线，且每条全文都在（不截断、不堆在一起）
        detail = self.app._format_snippets_detail(snippets)
        for i in (1, 2, 3):
            self.assertIn(f"【{i}】分类：", detail)
        self.assertIn("他睁开眼，发现自己来到了另一个世界。", detail)
        self.assertIn("第三条片段也必须完整出现", detail)
        self.assertIn("─", detail)  # 条目之间的分隔线
        # 空结果给出可操作提示
        empty_detail = self.app._format_snippets_detail([])
        self.assertIn("未抽取到片段", empty_detail)
        self.assertIn("错误，重来", empty_detail)

    def test_dialog_detail_text_scrollable_full_content(self):
        """明细区是只读 Text + 滚动条，长片段全文展示不截断。"""
        long_text = "月光洒在青石板上。" * 60  # 540 字，远超旧版 60 字预览上限
        snippets = [
            {"type": "景物", "categories": ["环境"], "text": long_text},
            {"type": "战争", "categories": ["战争"], "text": "短片段内容"},
        ]
        resp = queue.Queue()
        self.app._show_confirm_dialog(resp, "novel.txt", 1, 2, snippets)
        dlg = self.app._confirm_dlg
        texts = [w for w in self._all_children(dlg) if isinstance(w, tk.Text)]
        scrolls = [w for w in self._all_children(dlg) if isinstance(w, tk.ttk.Scrollbar)]
        self.assertEqual(len(texts), 1)
        self.assertEqual(len(scrolls), 1)  # 有滚动条
        content = texts[0].get("1.0", "end")
        self.assertIn(long_text, content)  # 全文展示，不截断
        self.assertIn("【1】", content)
        self.assertIn("【2】", content)
        self.assertIn("短片段内容", content)
        self.assertEqual(str(texts[0].cget("state")), "disabled")  # 只读，防误改
        self._find_button(dlg, "正确").invoke()
        self.assertEqual(resp.get(timeout=2), "ok")

    def test_dialog_close_button_disabled(self):
        resp = queue.Queue()
        self.app._show_confirm_dialog(resp, "a.txt", 1, 1, [])
        dlg = self.app._confirm_dlg
        # 调用“关闭窗口”协议处理器：必须是空操作，对话框仍在
        self.app._confirm_dialog_ignore_close()
        self.assertTrue(dlg.winfo_exists())
        # 仍可通过按钮正常结束
        self._find_button(dlg, "正确").invoke()
        self.assertEqual(resp.get(timeout=2), "ok")

    def test_dialog_shows_file_and_chunk_info(self):
        resp = queue.Queue()
        self.app._show_confirm_dialog(resp, "novel.txt", 3, 7,
                                      [{"type": "景物", "categories": ["环境"], "text": "z" * 30}])
        dlg = self.app._confirm_dlg
        labels = [w.cget("text") for w in self._all_children(dlg) if isinstance(w, tk.ttk.Label)]
        self.assertTrue(any("novel.txt" in t and "块 3/7" in t for t in labels))
        self._find_button(dlg, "正确").invoke()

    # ---------- 工作线程侧决策应用（打桩 _confirm_chunk，聚焦块循环逻辑） ----------

    def test_retry_reexecutes_chunk(self):
        """错误，重来 → 同一块重新调用 LLM，只存档一次。"""
        self.set_analysis_config()
        path = self.write_src("r.txt", "内容" * 5000)
        decisions = iter(["retry", "ok"])
        with mock.patch.object(self.app, "call_llm", return_value=self.CANNED) as m, \
             mock.patch.object(self.app, "_confirm_chunk",
                               side_effect=lambda *a, **k: next(decisions, "ok")):
            self.app.processing = True
            self.app.current_file, self.app.total_files = 1, 1
            total, failed, completed = self.app.process_file(path, "r.txt")
        self.assertEqual((total, failed, completed), (1, 0, True))
        self.assertEqual(m.call_count, 2)  # 重来一次
        self.assertEqual(len(os.listdir(os.path.join(self.wiki, "out", "环境"))), 1)

    def test_skip_chunk_continues_next_chunk(self):
        """跳过本段 → 本块不存档，继续后续块，并记入 analyze_log。"""
        self.set_analysis_config()
        path = self.write_src("s.txt", "内容" * 100000)  # 600KB → 4 块
        decisions = iter(["skip_chunk"])
        with mock.patch.object(self.app, "call_llm", return_value=self.CANNED), \
             mock.patch.object(self.app, "_confirm_chunk",
                               side_effect=lambda *a, **k: next(decisions, "ok")):
            self.app.processing = True
            self.app.current_file, self.app.total_files = 1, 1
            total, failed, completed = self.app.process_file(path, "s.txt")
        # 第 1 块跳过，其余 3 块正常入库
        self.assertEqual((total, failed, completed), (3, 0, True))
        self.assertEqual(len(os.listdir(os.path.join(self.wiki, "out", "环境"))), 3)
        content = open(self.log_file, encoding="utf-8").read()
        self.assertIn("s.txt 块 1/", content)
        self.assertIn("用户跳过本段", content)

    def test_skip_file_stops_remaining_chunks(self):
        """跳过本文 → 剩余块不再调用 LLM，文件仍算完整完成（可标记已处理）。"""
        self.set_analysis_config()
        path = self.write_src("f.txt", "内容" * 100000)  # 600KB → 4 块
        calls = {"n": 0}

        def counting(text, prompt):
            calls["n"] += 1
            return self.CANNED

        with mock.patch.object(self.app, "call_llm", side_effect=counting), \
             mock.patch.object(self.app, "_confirm_chunk", return_value="skip_file"):
            self.app.processing = True
            self.app.current_file, self.app.total_files = 1, 1
            total, failed, completed = self.app.process_file(path, "f.txt")
        self.assertEqual((total, failed, completed), (0, 0, True))
        self.assertEqual(calls["n"], 1)  # 后续块不再调用
        self.assertFalse(os.path.exists(os.path.join(self.wiki, "out")))
        content = open(self.log_file, encoding="utf-8").read()
        self.assertIn("f.txt 块 1/", content)
        self.assertIn("用户跳过本文", content)

    # ---------- 开关与配置 ----------

    def test_disabled_confirm_returns_ok_without_dialog(self):
        self.app.confirm_var.set(False)
        self.assertEqual(self.app._confirm_chunk("a.txt", 1, 1, []), "ok")
        self.assertIsNone(self.app._confirm_dlg)

    def test_confirm_checkbox_in_main_ui(self):
        self.assertIsInstance(self.app.confirm_check, tk.ttk.Checkbutton)
        self.assertEqual(self.app.confirm_check.cget("text"), "逐段确认")

    def test_confirm_flag_persisted(self):
        self.app.confirm_var.set(False)
        self.app.save_config()
        with open(nag.CONFIG_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["confirm_each_chunk"], "false")
        # 重新加载：开关恢复
        root2 = tk.Tk()
        root2.withdraw()
        try:
            app2 = nag.NovelAnalyzerApp(root2)
            self.assertFalse(app2.confirm_var.get())
        finally:
            root2.destroy()

    def test_confirm_default_on_when_missing(self):
        with open(nag.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"llm_url": "u", "wiki_dir": self.wiki}, f)
        root2 = tk.Tk()
        root2.withdraw()
        try:
            app2 = nag.NovelAnalyzerApp(root2)
            self.assertTrue(app2.confirm_var.get())  # 缺省开启
        finally:
            root2.destroy()

    # ---------- 真实队列往返（工作线程 ↔ 界面线程） ----------

    def test_confirm_chunk_roundtrip_via_queue(self):
        """_confirm_chunk 走真实队列：工作线程发请求 → 界面线程弹框 → 结果回传。"""
        self.app.confirm_var.set(True)
        result = {}

        def worker():
            result["decision"] = self.app._confirm_chunk("q.txt", 3, 7, [
                {"type": "穿越", "categories": ["穿越"], "text": "x" * 30}])

        t = threading.Thread(target=worker)
        t.start()
        # 主线程泵事件循环，等确认框出现后点击「正确」
        dlg = None
        deadline = time.time() + 10
        while time.time() < deadline:
            self.root.update()
            if self.app._confirm_dlg is not None:
                dlg = self.app._confirm_dlg
                break
            time.sleep(0.01)
        self.assertIsNotNone(dlg, "确认框未出现")
        self._find_button(dlg, "正确").invoke()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result.get("decision"), "ok")

    def test_skip_file_end_to_end_next_file_continues(self):
        """完整流程：第一个文件点「跳过本文」→ 标记已处理；第二个文件点「正确」→ 正常入库。"""
        self.set_analysis_config()
        self.write_src("aaa.txt", "内容" * 5000)
        self.write_src("bbb.txt", "内容" * 5000)
        self.app.input_path_var.set(self.src)
        self.app.confirm_var.set(True)
        script = ["跳过本文", "正确"]
        state = {"shown": 0}

        with mock.patch.object(self.app, "call_llm", return_value=self.CANNED):
            self.app.start_analysis()
            deadline = time.time() + 20
            while time.time() < deadline and self.app.processing:
                self.root.update()
                dlg = self.app._confirm_dlg
                if dlg is not None:
                    text = script[min(state["shown"], len(script) - 1)]
                    state["shown"] += 1
                    self._find_button(dlg, text).invoke()
                time.sleep(0.01)
            self.wait_finished()

        self.assertEqual(state["shown"], 2)  # 正好弹了两次
        self.assertIn("aaa.txt", self.app.processed_files)  # 跳过本文也标记已处理
        self.assertIn("bbb.txt", self.app.processed_files)
        env_dir = os.path.join(self.wiki, "out", "环境")
        self.assertEqual(len(os.listdir(env_dir)), 1)  # 只有 bbb 的片段入库
        log_text = self.app.log_text.get("1.0", "end")
        self.assertIn("用户跳过本文", log_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
