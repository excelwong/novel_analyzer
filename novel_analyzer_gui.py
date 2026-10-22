# -*- coding: utf-8 -*-
"""
小说片段提取器 GUI 应用
从小说文本中智能提取指定类型的片段，按分类存入 llm-wiki。
"""

import os
import json
import time
import re
import socket
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import urllib.request
import urllib.error

CONFIG_FILE = "novel_analyzer_config.json"
LLM_URL = "http://127.0.0.1:1234/v1/chat/completions"
LLM_MODEL = "qwen3.8-27b"
MAX_CHUNK_SIZE = 200 * 1024  # 200KB per chunk
MAX_SNIPPET_SIZE = 1000
# 段落确认框等待用户操作的上限（秒）：界面线程异常时的兜底，避免工作线程无限挂起
CONFIRM_TIMEOUT = 3600
# 段落确认框默认尺寸（可拉伸；抽取内容多时靠滚动条完整查看）
CONFIRM_DIALOG_W = 640
CONFIRM_DIALOG_H = 520

class NovelAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("小说片段提取器")
        self.root.geometry("1200x900")
        self.root.minsize(1100, 800)

        self.input_files = []
        self.processing = False
        self.current_file = 0
        self.total_files = 0
        self.total_snippets = 0
        self.processed_files = set()
        self.log_queue = queue.Queue()
        self.file_list_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.status_queue = queue.Queue()

        # 当前加载的分析配置
        self.config = None
        # 已处理文件日志文件路径（根据配置文件动态设置）
        self.processed_file_path = None

        # 加载 LLM 配置
        self.load_llm_config()

        # 创建配置变量
        self.llm_url_var = tk.StringVar(value=self.llm_config["llm_url"])
        self.llm_model_var = tk.StringVar(value=self.llm_config["llm_model"])
        self.temperature_var = tk.StringVar(value=self.llm_config["temperature"])
        self.max_tokens_var = tk.StringVar(value=self.llm_config["max_tokens"])
        self.source_dir_var = tk.StringVar(value=self.llm_config["source_dir"])
        self.wiki_dir_var = tk.StringVar(value=self.llm_config["wiki_dir"])

        # 配置文件选择变量
        self.config_path_var = tk.StringVar()
        self.config_name_var = tk.StringVar(value="未选择")

        # 逐段确认开关（界面复选框；运行中切换对后续块立即生效）
        self.confirm_var = tk.BooleanVar(value=self._confirm_flag_from_config())
        # 工作线程安全镜像：Tk 变量不能从非主线程读取（抛 RuntimeError），
        # 用 trace 把勾选状态同步到这个普通属性上，工作线程只读它
        self.confirm_each_chunk = bool(self.confirm_var.get())
        self.confirm_var.trace_add("write", self._sync_confirm_flag)
        # 当前打开的段落确认框（调试/测试用）
        self._confirm_dlg = None

        # 加载已处理文件列表
        self.load_processed_files()

        # 初始化目录结构
        self.init_directories()

        # 构建 GUI
        self.build_gui()

        # 定期处理队列
        self.root.after(100, self.process_log_queue)
        self.root.after(100, self.process_file_list_queue)
        self.root.after(100, self.process_progress_queue)
        self.root.after(100, self.process_status_queue)

    def load_llm_config(self):
        """加载 LLM 配置。"""
        defaults = {
            "llm_url": LLM_URL,
            "llm_model": LLM_MODEL,
            "temperature": "0.2",
            # 抽取任务只返回几百字 JSON，16384 已远超所需；过大只会让模型不停车时跑更久
            "max_tokens": "16384",
            "rate_limit": "1.0",
            "timeout": "600",
            "chunk_size": "200",
            # 逐段确认：每段抽取后弹窗（正确/错误重来/跳过本段/跳过本文），缺省开启
            "confirm_each_chunk": "true",
            "source_dir": "",
            "wiki_dir": ""
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if not isinstance(config, dict):
                        raise ValueError("配置根节点必须是 JSON 对象")
                    for key in defaults:
                        if key not in config:
                            config[key] = defaults[key]
                    self.llm_config = config
                    return
            except (json.JSONDecodeError, IOError, ValueError) as e:
                self.log(f"读取 LLM 配置失败，使用默认值：{e}")
        self.llm_config = defaults

    def save_config(self):
        """保存 LLM 配置。"""
        config = {
            "llm_url": self.llm_url_var.get().strip(),
            "llm_model": self.llm_model_var.get().strip(),
            "temperature": self.temperature_var.get().strip(),
            "max_tokens": self.max_tokens_var.get().strip(),
            "rate_limit": self.llm_config.get("rate_limit", "1.0"),
            "timeout": self.llm_config.get("timeout", "600"),
            "chunk_size": self.llm_config.get("chunk_size", "200"),
            "confirm_each_chunk": "true" if self.confirm_var.get() else "false",
            "source_dir": self.source_dir_var.get().strip(),
            "wiki_dir": self.wiki_dir_var.get().strip()
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            self.llm_config.update(config)
            self.log("配置已保存")
        except IOError as e:
            self.log(f"配置保存失败：{e}")

    def load_analysis_config(self, config_path):
        """加载分析配置文件。"""
        if not config_path or not os.path.exists(config_path):
            return None
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError("配置根节点必须是 JSON 对象")
            # 校验必填字段，缺字段时明确拒绝，避免分析到一半才崩溃
            missing = [k for k in ("prompt", "target_dir") if not config.get(k)]
            if missing:
                self.log(f"配置缺少必填字段：{', '.join(missing)}（需要 name/mode/prompt/target_dir）")
                messagebox.showwarning("配置无效", f"分析配置缺少必填字段：{', '.join(missing)}\n"
                                                   f"请检查配置文件内容。")
                self.config = None
                self.config_path_var.set("")
                self.config_name_var.set("未选择")
                return None
            if not self.llm_config.get("wiki_dir"):
                self.log("错误：尚未设置 Wiki 目录，请先在『配置 → LLM 设置』中填写")
                messagebox.showwarning("提示", "请先在『配置 → LLM 设置』中设置 Wiki 目录，"
                                               "否则分析结果无处保存。")
                return None
            self.config = config
            self.config_path_var.set(config_path)
            self.config_name_var.set(config.get("name", "未命名"))
            # 根据配置文件名设置已处理文件日志文件路径
            config_base = os.path.splitext(os.path.basename(config_path))[0]
            self.processed_file_path = os.path.join(self.llm_config["wiki_dir"], f"{config_base}.log")
            self.log(f"已加载分析配置：{config.get('name', '未命名')}")
            self.log(f"已处理文件日志：{self.processed_file_path}")
            # 加载该配置的已处理文件列表
            self.load_processed_files()
            return config
        except (json.JSONDecodeError, IOError, ValueError) as e:
            self.log(f"加载配置失败：{e}")
            return None

    def build_gui(self):
        # 菜单栏
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # 配置菜单
        config_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="配置", menu=config_menu)
        config_menu.add_command(label="LLM 设置", command=self.show_config_dialog)
        config_menu.add_command(label="保存 LLM 配置", command=self.save_config)

        # 顶部：输入路径和按钮
        top_frame = ttk.LabelFrame(self.root, text="输入")
        top_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(top_frame, text="输入路径：").grid(row=0, column=0, sticky="w", padx=5, pady=2)
        self.input_path_var = tk.StringVar(value=self.llm_config["source_dir"])
        self.input_path_entry = ttk.Entry(top_frame, textvariable=self.input_path_var, width=60)
        self.input_path_entry.grid(row=0, column=1, padx=5, pady=2, sticky="ew")
        ttk.Button(top_frame, text="选择文件...", command=self.browse_file).grid(row=0, column=2, padx=2)
        ttk.Button(top_frame, text="选择目录...", command=self.browse_directory).grid(row=0, column=3, padx=2)

        # 分析配置选择
        ttk.Label(top_frame, text="分析配置：").grid(row=1, column=0, sticky="w", padx=5, pady=2)
        self.config_entry = ttk.Entry(top_frame, textvariable=self.config_path_var, width=60, state="readonly")
        self.config_entry.grid(row=1, column=1, padx=5, pady=2, sticky="ew")
        ttk.Button(top_frame, text="选择配置...", command=self.browse_config).grid(row=1, column=2, padx=2)
        ttk.Label(top_frame, textvariable=self.config_name_var).grid(row=1, column=3, padx=2, sticky="w")

        # 按钮行
        btn_frame = ttk.Frame(top_frame)
        btn_frame.grid(row=2, column=0, columnspan=4, pady=5)
        self.start_btn = ttk.Button(btn_frame, text="开始分析", command=self.start_analysis)
        self.start_btn.pack(side="left", padx=2)
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self.stop_analysis)
        self.stop_btn.pack(side="left", padx=2)
        self.stop_btn.config(state="disabled")

        # 逐段确认开关
        self.confirm_check = ttk.Checkbutton(btn_frame, text="逐段确认", variable=self.confirm_var)
        self.confirm_check.pack(side="left", padx=(12, 2))
        ttk.Label(btn_frame, text="每段抽取后弹窗：正确 / 错误重来 / 跳过本段 / 跳过本文",
                  foreground="gray").pack(side="left")

        top_frame.columnconfigure(1, weight=1)

        # 中间区域：进度条
        progress_frame = ttk.LabelFrame(self.root, text="进度")
        progress_frame.pack(fill="x", padx=10, pady=5)

        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill="x", padx=5, pady=2)

        self.status_label = ttk.Label(progress_frame, text="就绪")
        self.status_label.pack(fill="x", padx=5, pady=2)

        # 下半部分：三个并排的面板
        bottom_frame = ttk.Frame(self.root)
        bottom_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # 左侧：已处理文件列表
        file_list_frame = ttk.LabelFrame(bottom_frame, text="已处理文件")
        file_list_frame.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=0)

        file_inner = ttk.Frame(file_list_frame)
        file_inner.pack(fill="both", expand=True, padx=5, pady=2)

        self.file_listbox = tk.Listbox(file_inner, height=20, width=40)
        self.file_listbox.pack(side="left", fill="both", expand=True)
        self.file_scroll = ttk.Scrollbar(file_inner, command=self.file_listbox.yview)
        self.file_scroll.pack(side="right", fill="y")
        self.file_listbox.config(yscrollcommand=self.file_scroll.set)

        # 右侧：处理日志
        log_frame = ttk.LabelFrame(bottom_frame, text="处理日志")
        log_frame.pack(side="right", fill="both", expand=True, padx=(5, 0), pady=0)

        log_inner = ttk.Frame(log_frame)
        log_inner.pack(fill="both", expand=True, padx=5, pady=2)

        self.log_text = tk.Text(log_inner, wrap="word", height=20)
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_scroll = ttk.Scrollbar(log_inner, command=self.log_text.yview)
        self.log_scroll.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=self.log_scroll.set)

    def browse_file(self):
        current_path = self.input_path_var.get().strip()
        if not current_path or not os.path.exists(current_path):
            current_path = self.llm_config["source_dir"]
        # initialdir 需要是目录：当前值是文件时取其父目录
        if current_path and os.path.isfile(current_path):
            current_path = os.path.dirname(current_path)
        path = filedialog.askopenfilename(
            initialdir=current_path,
            title="选择小说文件",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")]
        )
        if path:
            self.input_path_var.set(path)

    def browse_directory(self):
        current_path = self.input_path_var.get().strip()
        if not current_path or not os.path.exists(current_path):
            current_path = self.llm_config["source_dir"]
        path = filedialog.askdirectory(initialdir=current_path, title="选择小说目录")
        if path:
            self.input_path_var.set(path)

    def browse_config(self):
        path = filedialog.askopenfilename(
            initialdir=os.path.dirname(os.path.abspath(__file__)),
            title="选择分析配置文件",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if path:
            self.load_analysis_config(path)

    def show_config_dialog(self):
        """显示 LLM 配置对话框。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("LLM 配置")
        dialog.geometry("500x480")
        dialog.transient(self.root)
        dialog.grab_set()

        ttk.Label(dialog, text="LLM URL：").grid(row=0, column=0, sticky="w", padx=10, pady=5)
        url_var = tk.StringVar(value=self.llm_url_var.get())
        ttk.Entry(dialog, textvariable=url_var, width=45).grid(row=0, column=1, padx=5, pady=5, sticky="ew")

        ttk.Label(dialog, text="模型：").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        model_var = tk.StringVar(value=self.llm_model_var.get())
        ttk.Entry(dialog, textvariable=model_var, width=30).grid(row=1, column=1, padx=5, pady=5)

        ttk.Label(dialog, text="温度：").grid(row=2, column=0, sticky="w", padx=10, pady=5)
        temp_var = tk.StringVar(value=self.temperature_var.get())
        ttk.Entry(dialog, textvariable=temp_var, width=10).grid(row=2, column=1, padx=5, pady=5)

        ttk.Label(dialog, text="最大 Tokens：").grid(row=3, column=0, sticky="w", padx=10, pady=5)
        tokens_var = tk.StringVar(value=self.max_tokens_var.get())
        ttk.Entry(dialog, textvariable=tokens_var, width=10).grid(row=3, column=1, padx=5, pady=5)

        ttk.Label(dialog, text="请求间隔(秒)：").grid(row=4, column=0, sticky="w", padx=10, pady=5)
        rate_var = tk.StringVar(value=self.llm_config.get("rate_limit", "1.0"))
        ttk.Entry(dialog, textvariable=rate_var, width=10).grid(row=4, column=1, padx=5, pady=5)

        ttk.Label(dialog, text="请求超时(秒)：").grid(row=5, column=0, sticky="w", padx=10, pady=5)
        timeout_var = tk.StringVar(value=self.llm_config.get("timeout", "600"))
        ttk.Entry(dialog, textvariable=timeout_var, width=10).grid(row=5, column=1, padx=5, pady=5)
        ttk.Label(dialog, text="单次请求无响应的最长等待，超时按块失败记入 analyze_log",
                  foreground="gray").grid(row=5, column=2, sticky="w", padx=2)

        ttk.Label(dialog, text="分块大小(KB)：").grid(row=6, column=0, sticky="w", padx=10, pady=5)
        chunk_var = tk.StringVar(value=self.llm_config.get("chunk_size", "200"))
        ttk.Entry(dialog, textvariable=chunk_var, width=10).grid(row=6, column=1, padx=5, pady=5)
        ttk.Label(dialog, text="超过此大小的文件按段落分块；越小单请求越快，最小 16",
                  foreground="gray").grid(row=6, column=2, sticky="w", padx=2)

        ttk.Label(dialog, text="源目录：").grid(row=7, column=0, sticky="w", padx=10, pady=5)
        source_var = tk.StringVar(value=self.source_dir_var.get())
        ttk.Entry(dialog, textvariable=source_var, width=45).grid(row=7, column=1, padx=5, pady=5, sticky="ew")

        ttk.Label(dialog, text="Wiki 目录：").grid(row=8, column=0, sticky="w", padx=10, pady=5)
        wiki_var = tk.StringVar(value=self.wiki_dir_var.get())
        ttk.Entry(dialog, textvariable=wiki_var, width=45).grid(row=8, column=1, padx=5, pady=5, sticky="ew")

        def save_and_close():
            # 数字字段校验，避免填错后在调用时才报错
            try:
                float(temp_var.get().strip())
                int(tokens_var.get().strip())
                float(rate_var.get().strip())
                float(timeout_var.get().strip())
                chunk_kb = int(float(chunk_var.get().strip()))
                if chunk_kb < 16:
                    raise ValueError("分块大小不能小于 16KB")
            except ValueError:
                messagebox.showwarning("提示", "温度、最大 Tokens、请求间隔、请求超时必须是数字；\n"
                                               "分块大小必须是 ≥16 的整数（KB）！")
                return
            self.llm_url_var.set(url_var.get().strip())
            self.llm_model_var.set(model_var.get().strip())
            self.temperature_var.set(temp_var.get().strip())
            self.max_tokens_var.set(tokens_var.get().strip())
            self.llm_config["rate_limit"] = rate_var.get().strip()
            self.llm_config["timeout"] = timeout_var.get().strip()
            self.llm_config["chunk_size"] = str(chunk_kb)
            self.source_dir_var.set(source_var.get().strip())
            self.wiki_dir_var.set(wiki_var.get().strip())
            self.save_config()
            # Wiki 目录可能刚填，重新初始化目录结构
            self.init_directories()
            dialog.destroy()

        ttk.Button(dialog, text="保存", command=save_and_close).grid(row=9, column=0, padx=5, pady=10)
        ttk.Button(dialog, text="取消", command=dialog.destroy).grid(row=9, column=1, padx=5, pady=10, sticky="w")
        dialog.columnconfigure(1, weight=1)

    def load_processed_files(self):
        """从配置文件对应的日志文件中加载已处理文件列表。"""
        self.processed_files = set()
        if self.processed_file_path and os.path.exists(self.processed_file_path):
            try:
                with open(self.processed_file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.processed_files.add(line)
                self.log(f"已加载 {len(self.processed_files)} 个已处理文件")
            except IOError as e:
                self.log(f"读取已处理文件列表失败：{e}")
        # 回填到左侧列表（GUI 已构建时）
        if hasattr(self, "file_listbox"):
            self._refresh_processed_listbox()

    def _refresh_processed_listbox(self):
        """把已处理文件列表同步到左侧列表框。"""
        self.file_listbox.delete(0, "end")
        for fn in sorted(self.processed_files):
            self.file_listbox.insert("end", fn)

    def reset_processed_files(self):
        """重置已处理文件列表（内存中）。"""
        self.processed_files.clear()

    def save_processed_file(self, filename):
        """将已处理文件名保存到配置文件对应的日志文件中。"""
        if filename not in self.processed_files:
            self.processed_files.add(filename)
            if self.processed_file_path:
                try:
                    with open(self.processed_file_path, "a", encoding="utf-8") as f:
                        f.write(filename + "\n")
                except IOError as e:
                    self.log(f"保存已处理文件记录失败：{e}")
            self.file_list_queue.put(filename)

    def init_directories(self):
        """初始化 wiki 目录结构。"""
        wiki_dir = self.llm_config["wiki_dir"]
        if not wiki_dir:
            self.log("警告：尚未设置 Wiki 目录（请在『配置 → LLM 设置』中填写），结果将无法保存")
            return
        os.makedirs(wiki_dir, exist_ok=True)
        log_path = os.path.join(wiki_dir, "analyze_log.txt")
        if not os.path.exists(log_path):
            open(log_path, "w", encoding="utf-8").close()
        self.log("Wiki 目录结构已初始化")

    def _append_analyze_log(self, message):
        """立即向 Wiki 目录的 analyze_log.txt 追加一行（带时间戳）。

        用于块调用失败/超时等需要事后排查的事件：GUI 日志关窗即丢，
        这个文件持久化在 Wiki 目录里，每次写入都直接落盘。
        """
        wiki_dir = self.llm_config.get("wiki_dir")
        if not wiki_dir:
            return
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        try:
            with open(os.path.join(wiki_dir, "analyze_log.txt"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            self.log(f"写入 analyze_log.txt 失败：{e}")

    def log(self, message):
        self.log_queue.put(message)

    def process_log_queue(self):
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.insert("end", message + "\n")
                self.log_text.see("end")
        except queue.Empty:
            pass
        finally:
            # 防止长时间运行后日志框无限增长（超过 6000 行删除最旧的 1000 行）
            try:
                total_lines = int(self.log_text.index("end-1c").split(".")[0])
                if total_lines > 6000:
                    self.log_text.delete("1.0", "1001.0")
            except Exception:
                pass
            self.root.after(100, self.process_log_queue)

    def process_file_list_queue(self):
        try:
            while True:
                filename = self.file_list_queue.get_nowait()
                self.file_listbox.insert("end", filename)
                self.file_listbox.see("end")
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_file_list_queue)

    def process_progress_queue(self):
        try:
            while True:
                progress = self.progress_queue.get_nowait()
                self.progress_var.set(progress)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_progress_queue)

    def process_status_queue(self):
        try:
            while True:
                status = self.status_queue.get_nowait()
                if isinstance(status, tuple) and status and status[0] == "confirm_chunk":
                    # 工作线程请求弹出段落确认框：在界面线程创建，避免跨线程操作 Tk
                    self._show_confirm_dialog(*status[1])
                elif status == "restore_buttons":
                    # 分析结束：恢复按钮状态（经队列转到界面线程执行，避免跨线程操作 Tk）
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                else:
                    self.status_label.config(text=status)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.process_status_queue)

    # ---------- 逐段确认 ----------

    def _confirm_flag_from_config(self):
        """从配置读取逐段确认开关；缺省开启（每次段落抽取后都确认）。"""
        v = self.llm_config.get("confirm_each_chunk", "true")
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def _sync_confirm_flag(self, *_args):
        """复选框变化时同步到普通属性（工作线程只能读这个，不能读 Tk 变量）。"""
        try:
            self.confirm_each_chunk = bool(self.confirm_var.get())
        except Exception:
            pass

    def _confirm_enabled(self):
        """是否开启逐段确认。读普通属性镜像，工作线程安全。"""
        return bool(getattr(self, "confirm_each_chunk", False))

    def _confirm_chunk(self, filename, chunk_no, chunk_total, snippets):
        """（工作线程）请求界面线程弹出段落确认框，阻塞等待用户决定。

        返回 "ok"（正确，存档继续）/ "retry"（错误，重来本块）/
        "skip_chunk"（跳过本段）/ "skip_file"（跳过本文）。
        关闭逐段确认时直接放行，不弹框。
        """
        if not self._confirm_enabled():
            return "ok"
        resp = queue.Queue()
        self.status_queue.put(("confirm_chunk", (resp, filename, chunk_no, chunk_total, snippets)))
        try:
            decision = resp.get(timeout=CONFIRM_TIMEOUT)
        except queue.Empty:
            # 界面线程异常时的兜底：不冒险存档，按跳过本段处理并留痕
            self.log("  警告：段落确认框等待超时，本块按跳过处理")
            self._append_analyze_log(
                f"{filename} 块 {chunk_no}/{chunk_total or '?'} 确认超时，按跳过处理")
            return "skip_chunk"
        return decision if decision in ("ok", "retry", "skip_chunk", "skip_file") else "ok"

    @staticmethod
    def _confirm_summary(snippets):
        """确认框头部的一行统计：片段数量 + 分类计数。"""
        if not snippets:
            return "共 0 个片段（本块未抽取到内容）"
        cats = {}
        for sn in snippets:
            for c in dict.fromkeys(sn.get("categories") or []):
                cats[c] = cats.get(c, 0) + 1
        head = f"共 {len(snippets)} 个片段"
        if cats:
            head += "：" + "、".join(f"{c}×{n}" for c, n in cats.items())
        return head

    @staticmethod
    def _format_snippets_detail(snippets):
        """把全部片段格式化为分条目明细（确认框滚动区完整展示，不截断）。"""
        if not snippets:
            return ("未抽取到片段。\n\n"
                    "若上方日志有解析失败记录，可点「错误，重来」重试本块。")
        parts = []
        for idx, sn in enumerate(snippets, 1):
            cats = list(dict.fromkeys(sn.get("categories") or []))
            cat_str = "/".join(cats) if cats else "未分类"
            raw = sn.get("text")
            text = raw.strip() if isinstance(raw, str) else str(raw or "")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if not text:
                text = "（片段文本为空）"
            word_count = len(text.replace("\n", "").replace(" ", ""))
            parts.append(f"【{idx}】分类：{cat_str}　字数：{word_count}\n"
                         f"{'─' * 34}\n"
                         f"{text}")
        return "\n\n".join(parts)

    def _confirm_dialog_ignore_close(self):
        """确认框必须四选一：右上角关闭设为空操作。"""

    def _show_confirm_dialog(self, resp, filename, chunk_no, chunk_total, snippets):
        """（界面线程）弹出段落抽取确认框；用户四选一，结果经 resp 队列返回工作线程。

        明细区完整展示每条抽取片段（分条目排版，内容多时可滚动、窗口可拉伸）。
        正确=存档并继续；错误，重来=重新执行本块抽取；跳过本段=本块不存档；
        跳过本文=本文件剩余块不再分析（文件仍标记为已处理，避免重跑重复入库）。
        """
        dialog = None
        try:
            dialog = tk.Toplevel(self.root)
            dialog.title("段落抽取确认")
            dialog.transient(self.root)
            dialog.geometry(f"{CONFIRM_DIALOG_W}x{CONFIRM_DIALOG_H}")
            dialog.minsize(480, 360)
            dialog.resizable(True, True)

            ttk.Label(dialog, text=f"文件：{filename}    块 {chunk_no}/{chunk_total or '?'}",
                      font=("", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 2))
            ttk.Label(dialog, text=self._confirm_summary(snippets)).pack(anchor="w", padx=12, pady=2)

            # 明细区：全部片段分条目完整展示，内容超出窗口时用滚动条查看
            detail_frame = ttk.Frame(dialog)
            detail_frame.pack(fill="both", expand=True, padx=12, pady=6)
            detail_text = tk.Text(detail_frame, wrap="word", state="disabled",
                                  relief="solid", borderwidth=1, padx=6, pady=4)
            detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical",
                                          command=detail_text.yview)
            detail_text.config(yscrollcommand=detail_scroll.set)
            detail_scroll.pack(side="right", fill="y")
            detail_text.pack(side="left", fill="both", expand=True)
            detail_text.config(state="normal")
            detail_text.insert("1.0", self._format_snippets_detail(snippets))
            detail_text.config(state="disabled")

            btn_row = ttk.Frame(dialog)
            btn_row.pack(fill="x", padx=12, pady=(6, 10))

            def choose(value):
                resp.put(value)
                self._confirm_dlg = None
                dialog.destroy()

            ttk.Button(btn_row, text="正确", width=10,
                       command=lambda: choose("ok")).pack(side="left", padx=3)
            ttk.Button(btn_row, text="错误，重来", width=10,
                       command=lambda: choose("retry")).pack(side="left", padx=3)
            ttk.Button(btn_row, text="跳过本段", width=10,
                       command=lambda: choose("skip_chunk")).pack(side="left", padx=3)
            ttk.Button(btn_row, text="跳过本文", width=10,
                       command=lambda: choose("skip_file")).pack(side="left", padx=3)

            ttk.Label(dialog,
                      text="「跳过本文」后本文件剩余块不再分析，文件标记为已处理（不会重复入库）",
                      foreground="gray", wraplength=460).pack(anchor="w", padx=12, pady=(0, 10))

            # 必须四选一：禁用右上角关闭
            dialog.protocol("WM_DELETE_WINDOW", self._confirm_dialog_ignore_close)
            # 居中于主窗口（默认尺寸固定，直接按常量计算）
            rw, rh = self.root.winfo_width(), self.root.winfo_height()
            rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
            dialog.geometry("+%d+%d" % (max(0, rx + (rw - CONFIRM_DIALOG_W) // 2),
                                        max(0, ry + max(0, (rh - CONFIRM_DIALOG_H) // 3))))
            dialog.grab_set()
            dialog.focus_set()
            self._confirm_dlg = dialog
        except Exception as e:
            # 确认框创建失败时不阻塞分析流水线：按“正确”继续并留痕
            self.log(f"  警告：段落确认框创建失败（{e}），本块按正确继续")
            if dialog is not None:
                try:
                    dialog.destroy()
                except Exception:
                    pass
            self._confirm_dlg = None
            resp.put("ok")

    def get_llm_url(self):
        return self.llm_url_var.get().strip()

    def get_llm_model(self):
        return self.llm_model_var.get().strip()

    @staticmethod
    def _is_llm_error(result):
        """判断 LLM 返回是否为调用错误（而非正文内容恰好含 Error 字样）。"""
        if not result:
            return True
        return result.startswith("Error:") or result.startswith("HTTP Error")

    def call_llm(self, text, prompt):
        # 注意：本方法在工作线程中被调用。Tk 变量不能跨线程读取（会抛
        # RuntimeError "main thread is not in main loop"），
        # 因此统一从 llm_config（普通 dict，保存配置时已同步）读取参数。
        url = self.llm_config.get("llm_url", LLM_URL)
        model = self.llm_config.get("llm_model", LLM_MODEL)
        temperature = float(self.llm_config.get("temperature", "0.2"))
        max_tokens = int(self.llm_config.get("max_tokens", "16384"))

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": text}
        ]

        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        # 请求间隔（限速），可在配置中调整
        try:
            delay = float(self.llm_config.get("rate_limit", "1.0"))
        except (TypeError, ValueError):
            delay = 1.0
        if delay > 0:
            time.sleep(delay)

        # 请求超时（秒），可在配置中调整；非流式下它实际约束整段生成的时长
        try:
            timeout = float(self.llm_config.get("timeout", "600"))
        except (TypeError, ValueError):
            timeout = 600.0

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            return f"HTTP Error {e.code}: {detail}"
        except (socket.timeout, TimeoutError) as e:
            # 超时单独打标记，便于在 analyze_log 里与普通调用失败区分
            return f"Error: TIMEOUT: {e}"
        except Exception as e:
            return f"Error: {str(e)}"

    def read_file(self, path):
        if not os.path.exists(path):
            self.log(f"  跳过：文件不存在 {path}")
            return ""
        for enc in ["utf-8-sig", "utf-8", "gbk", "gb2312", "cp936"]:
            try:
                with open(path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
            except OSError as e:
                # 读取失败（被占用/无权限等）：记日志后交给兜底分支，绝不向上抛
                self.log(f"  警告：以 {enc} 读取失败：{e}")
                continue
        # 全部编码尝试失败：用 utf-8 + replace 兜底，尽量保住内容而不是静默丢整个文件
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                self.log("  警告：编码识别失败，已按 UTF-8 容错模式读取（可能有少量乱码）")
                return f.read()
        except OSError as e:
            self.log(f"  读取文件失败：{e}")
            return ""

    @staticmethod
    def _extract_json_objects(s):
        """按花括号深度（感知字符串与转义）提取所有顶层 {...} 对象。"""
        objs, depth, start = [], 0, -1
        in_str, esc = False, False
        for i, ch in enumerate(s):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start >= 0:
                        objs.append(s[start:i + 1])
        return objs

    def _log_parse_failure(self, filename, chunk_no, chunk_total, reason):
        """解析失败：只记一行摘要（时间/错误类型/涉及文件+块号），绝不原文回填。"""
        where = filename or "未知文件"
        if chunk_no:
            where += f" 块 {chunk_no}/{chunk_total or '?'}"
        self.log(f"    解析失败：{reason}")
        self._append_analyze_log(f"{where} 解析失败: {reason}")

    def parse_llm_result(self, result, filename="", chunk_no=0, chunk_total=0):
        """解析 LLM 返回的结果，提取 snippets。

        filename/chunk_no/chunk_total 仅用于失败时定位（写入 analyze_log.txt）；
        解析失败只记录一行摘要，不回填 LLM 原文，保证日志可读。
        """
        if not result or self._is_llm_error(result):
            return []

        # Remove markdown code block delimiters
        clean_result = result.replace("```json", "").replace("```", "").strip()

        # Find the first { and last }
        start = clean_result.find("{")
        end = clean_result.rfind("}")
        if start < 0 or end <= start:
            self.log("No JSON object found in LLM response")
            self._log_parse_failure(filename, chunk_no, chunk_total, "响应中未找到 JSON 结构")
            return []

        json_str = clean_result[start:end + 1]

        # 弯引号 -> ASCII 引号的“修复版”：只在 LLM 用全角引号做定界符时用得上。
        # 注意不能无条件先替换——小说正文里经常出现“ ”字样，先替换会把合法 JSON 打坏，
        # 因此原串始终是第一候选，替换版仅作兜底。
        curly_str = json_str
        for bad, good in [("\u201c", '"'), ("\u201d", '"'), ("“", '"'), ("”", '"'),
                          ("\u2018", "'"), ("\u2019", "'"), ("‘", "'"), ("’", "'")]:
            curly_str = curly_str.replace(bad, good)

        # 依次尝试：原串 / 原串修尾随逗号 / 弯引号替换版 / 替换版再修尾随逗号
        data = None
        for candidate in (json_str,
                          re.sub(r",\s*([}\]])", r"\1", json_str),
                          curly_str,
                          re.sub(r",\s*([}\]])", r"\1", curly_str)):
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue

        if isinstance(data, dict):
            snippets = data.get("snippets")
            if isinstance(snippets, list):
                return [s for s in snippets if isinstance(s, dict)]
        elif isinstance(data, list):
            return [s for s in data if isinstance(s, dict)]

        # Fallback: extract each {...} object by brace-depth pairing (safe for
        # text fields containing '}' or fields in any order), then parse one by one.
        # 先用原串（保留正文里的弯引号），没有结果再试弯引号替换版
        for candidate in (json_str, curly_str):
            snippets = []
            for obj_str in self._extract_json_objects(candidate):
                try:
                    obj = json.loads(obj_str)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and "type" in obj:
                    obj.setdefault("categories", [])
                    obj.setdefault("text", "")
                    snippets.append(obj)
            if snippets:
                self.log(f"Extracted {len(snippets)} snippets using brace-pairing fallback")
                return snippets

        # 所有解析策略均失败：只在 analyze_log.txt 记一行摘要（时间/类型/文件+块号/原因），
        # 不回填 LLM 原文——原文往往是裹着坏 JSON 的大段小说，回填会让日志无法阅读
        if isinstance(data, dict):
            reason = "JSON 结构不符（缺少 snippets 数组）"
        else:
            reason = "JSON 解析失败（可能被 max_tokens 截断或格式损坏）"
        self._log_parse_failure(filename, chunk_no, chunk_total, reason)

        return []

    def save_snippet(self, category_dir, content, source):
        os.makedirs(category_dir, exist_ok=True)

        # Get directory name as prefix
        category_name = os.path.basename(category_dir)

        # 一次扫描取最大编号 +1（避免逐个探测的 O(n^2)）
        seq = 1
        pat = re.compile(r"^%s-(\d{5})\.md$" % re.escape(category_name))
        try:
            for fn in os.listdir(category_dir):
                mm = pat.match(fn)
                if mm:
                    seq = max(seq, int(mm.group(1)) + 1)
        except OSError:
            pass

        filename = f"{category_name}-{seq:05d}.md"
        filepath = os.path.join(category_dir, filename)

        # 将内容中的换行符替换为空格，避免出现字面量的 \n
        clean_content = content.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        clean_content = clean_content.replace("\\n", " ")
        clean_content = " ".join(clean_content.split())  # 合并多个空格

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# {category_name}片段 {seq:05d}\n\n")
            f.write(f"**来源**：{source}\n\n")
            f.write("---\n\n")
            f.write(clean_content.strip())
            f.write("\n\n---\n")

        return seq

    def _chunk_size_bytes(self):
        """分块大小（字节）：从配置读取，非法值回退默认，并设 16KB 下限防止误配。"""
        try:
            kb = int(float(self.llm_config.get("chunk_size", MAX_CHUNK_SIZE // 1024)))
        except (TypeError, ValueError):
            kb = MAX_CHUNK_SIZE // 1024
        return max(16, kb) * 1024

    def split_text_into_chunks(self, text):
        """将大文本按段落分割成更小的块，确保语义完整性。"""
        limit = self._chunk_size_bytes()
        # Use byte size instead of character size
        text_bytes = text.encode("utf-8")
        if len(text_bytes) <= limit:
            return [text]

        # 按段落分割（支持 \r\n, \n, \r 换行符）
        paragraphs = re.split(r'[\r\n]+', text)
        # Filter out empty paragraphs
        paragraphs = [p.strip() for p in paragraphs if p.strip()]

        chunks = []
        current_chunk = ""

        for para in paragraphs:
            para_bytes = para.encode("utf-8")

            # If a single paragraph is too large, split it
            if len(para_bytes) > limit:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""
                # limit 是字节数；中文 UTF-8 每字最多 4 字节，
                # 按 4 字节/字符的保守预算换算，确保切出的块不会超出字节上限
                char_budget = max(1024, limit // 4)
                pos = 0
                while pos < len(para):
                    chunks.append(para[pos:pos + char_budget])
                    pos += char_budget
                continue

            current_bytes = current_chunk.encode("utf-8") if current_chunk else b""
            # 如果加上这个段落会超过限制，保存当前块并开始新的块
            if len(current_bytes) + len(para_bytes) > limit:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = para
            else:
                if current_chunk:
                    current_chunk += "\n\n" + para
                else:
                    current_chunk = para

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def process_file(self, filepath, filename):
        """分析单个文件。

        返回 (total, failed, completed)：
          total     - 成功入库的片段数
          failed    - LLM 调用失败的块数（这些块不重跑，仅在日志中记录，
                      以免重跑导致已成功的块重复入库）
          completed - 是否完整跑完。被中止、读取失败、配置缺失时为 False，
                      调用方据此不把文件标记为已处理；块失败不影响 completed，
                      文件仍照常标记为已处理。

        逐段确认（confirm_var，缺省开启）：每个块抽取成功后弹窗四选一——
        正确（存档继续）/ 错误，重来（重新执行本块抽取）/ 跳过本段（本块不存档）/
        跳过本文（剩余块不再分析，文件仍标记为已处理，不重复入库）。
        """
        self.log(f"\n===== 分析文件：{filename} =====")

        text = self.read_file(filepath)
        if not text:
            self.log(f"  跳过：无法读取文件")
            return 0, 0, False

        # 智能分块
        chunks = self.split_text_into_chunks(text)

        if len(chunks) > 1:
            self.log(f"  大文件，拆分为 {len(chunks)} 个块")

        total = 0
        failed = 0
        skipped = 0        # 用户主动跳过（跳过本段/跳过本文）的块数
        skip_file = False  # 用户选择了「跳过本文」
        completed = True
        category_counts = {}

        # 根据当前配置执行分析（启动时已校验，这里再防御一次）
        prompt = self.config.get("prompt")
        target_dir = self.config.get("target_dir")
        if not prompt or not target_dir:
            self.log("  错误：分析配置缺少 prompt 或 target_dir 字段")
            return 0, len(chunks), False

        wiki_dir = self.llm_config["wiki_dir"]

        for i, chunk in enumerate(chunks, 1):
            # 块间也响应「停止」：大文件分很多块时，点完停止不用干等整个文件跑完
            if not self.processing:
                self.log("  已中止，剩余文本块跳过")
                completed = False
                break
            self.log(f"  分析文本块 {i}/{len(chunks)}...")
            # 块级进度：让大文件的进度条也平滑推进
            base = (self.current_file - 1) / max(self.total_files, 1) * 100
            self.progress_queue.put(base + (i - 0.5) / len(chunks) * 100 / max(self.total_files, 1))

            # 内层循环：支持用户在确认框点「错误，重来」后重新执行本块的抽取指令
            while True:
                result = self.call_llm(chunk, prompt)
                if self._is_llm_error(result):
                    failed += 1
                    # 超时与普通调用失败分开标记，立即落盘到 analyze_log.txt
                    kind = "超时" if result.startswith("Error: TIMEOUT") else "调用失败"
                    self.log(f"    LLM 错误：{result[:100]}")
                    self._append_analyze_log(
                        f"{filename} 块 {i}/{len(chunks)} {kind}: {result[:500]}")
                    break  # 调用失败没有可确认的抽取结果，按既有失败策略记日志后继续

                snippets = self.parse_llm_result(result, filename, i, len(chunks))
                decision = self._confirm_chunk(filename, i, len(chunks), snippets)

                if decision == "retry":
                    if not self.processing:
                        completed = False
                        break
                    self.log(f"  块 {i}/{len(chunks)}：用户选择重来，重新执行抽取")
                    continue
                if decision == "skip_chunk":
                    skipped += 1
                    self.log(f"  块 {i}/{len(chunks)}：用户跳过本段，内容不抽取")
                    self._append_analyze_log(f"{filename} 块 {i}/{len(chunks)} 用户跳过本段")
                    break
                if decision == "skip_file":
                    skipped += 1
                    skip_file = True
                    self.log(f"  块 {i}/{len(chunks)}：用户跳过本文，剩余块不再分析")
                    self._append_analyze_log(
                        f"{filename} 块 {i}/{len(chunks)} 用户跳过本文，剩余块不再分析")
                    break

                # decision == "ok"：确认无误，存档
                for snippet in snippets:
                    snippet_cats = snippet.get("categories", [])
                    text_content = snippet.get("text", "").strip()

                    if not text_content or len(text_content) < 20:
                        continue

                    if len(text_content) > MAX_SNIPPET_SIZE:
                        text_content = text_content[:MAX_SNIPPET_SIZE] + "..."

                    # categories 去重，避免同一片段重复存档
                    for cat in dict.fromkeys(snippet_cats):
                        cat_dir = os.path.join(wiki_dir, target_dir, cat)
                        seq = self.save_snippet(cat_dir, text_content, filename)
                        total += 1
                        category_counts[cat] = category_counts.get(cat, 0) + 1
                        self.log(f"      {cat}+1")
                break

            if skip_file:
                break

        # 显示分类统计
        self.log(f"  完成，共 {total} 个片段")
        if category_counts:
            count_parts = []
            for cat, count in category_counts.items():
                count_parts.append(f"{cat}:{count}")
            self.log(f"  分类统计：{', '.join(count_parts)}")
        if failed:
            self.log(f"  警告：{failed}/{len(chunks)} 个块调用失败，这些块的内容未抽取"
                     f"（文件仍会标记为已处理，不自动重跑，以免已成功的块重复入库）")
        if skipped:
            self.log(f"  注意：{skipped}/{len(chunks)} 个块被用户跳过，未抽取")
        if skip_file:
            self.log(f"  已按用户要求跳过本文：剩余 {len(chunks) - i} 个块未分析"
                     f"（文件仍标记为已处理，不会重复入库）")
        self.log("")

        return total, failed, completed

    def start_analysis(self):
        # 检查是否已加载分析配置
        if not self.config:
            messagebox.showwarning("提示", "请先选择分析配置文件！")
            self.log("请先选择分析配置文件！")
            return

        if not self.llm_config.get("wiki_dir"):
            messagebox.showwarning("提示", "请先在『配置 → LLM 设置』中设置 Wiki 目录，否则无法保存结果！")
            self.log("错误：尚未设置 Wiki 目录")
            return

        # 获取输入路径（所有校验都在置 processing=True 之前，避免失败路径卡死按钮状态）
        input_path = self.input_path_var.get().strip()
        if not input_path:
            messagebox.showwarning("提示", "请先选择输入文件或目录！")
            return
        if not os.path.exists(input_path):
            messagebox.showwarning("提示", f"输入路径不存在：{input_path}")
            self.log(f"输入路径不存在：{input_path}")
            return

        self.processing = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status_queue.put("分析中...")

        # 收集所有 txt 文件
        self.input_files = []
        if os.path.isfile(input_path):
            if input_path.lower().endswith(".txt"):
                self.input_files.append((input_path, os.path.basename(input_path)))
        elif os.path.isdir(input_path):
            for root, dirs, files in os.walk(input_path):
                for fname in files:
                    if fname.lower().endswith(".txt"):
                        self.input_files.append((os.path.join(root, fname), fname))

        self.input_files.sort(key=lambda x: x[1])

        self.total_files = len(self.input_files)
        self.current_file = 0
        self.total_snippets = 0

        if self.total_files == 0:
            self.log("没有找到 txt 文件")
            self.processing = False
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            return

        self.log(f"找到 {self.total_files} 个 txt 文件，开始分析...")
        self.log(f"分析模式：{self.config.get('name', '未命名')}")

        # 在后台线程中处理
        thread = threading.Thread(target=self.process_files)
        thread.daemon = True
        thread.start()

    def process_files(self):
        for filepath, filename in self.input_files:
            if not self.processing:
                break

            self.current_file += 1
            progress = (self.current_file / self.total_files) * 100
            self.progress_queue.put(progress)
            self.status_queue.put(f"处理中 [{self.current_file}/{self.total_files}]")

            # 跳过已处理的文件
            if filename in self.processed_files:
                self.log(f"跳过已处理文件：{filename}")
                continue

            try:
                count, failed, completed = self.process_file(filepath, filename)
                self.total_snippets += count
                # 只要所有块都尝试过（completed），即使有块失败也标记为已处理：
                # 失败块仅在日志中记录，不重跑——重跑会把已成功的块重复入库，
                # 手工去重的成本高于丢掉几个失败块
                if completed:
                    self.save_processed_file(filename)
                    if failed:
                        self.log(f"  注意：{filename} 有 {failed} 个块处理失败，未抽取的内容不会自动补齐")
                else:
                    self.log(f"  {filename} 未完整完成（被中止或无法读取），不标记为已处理，下次可重跑补齐")
            except Exception as e:
                self.log(f"文件处理错误：{filename} - {e}")

        finished = self.processing
        if finished:
            self.log(f"分析完成！共处理 {self.current_file}/{self.total_files} 个文件，提取 {self.total_snippets} 个片段。")
        else:
            self.log(f"已中止。已处理 {self.current_file}/{self.total_files} 个文件，提取 {self.total_snippets} 个片段。")
        self.processing = False
        self.status_queue.put("分析完成" if finished else "已中止")
        self.status_queue.put("restore_buttons")

    def stop_analysis(self):
        self.processing = False
        self.stop_btn.config(state="disabled")


def main():
    root = tk.Tk()
    app = NovelAnalyzerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
