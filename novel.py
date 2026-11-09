#!/usr/bin/env python3
"""Local, dependency-free workflow controller. The agent supplies prose and reviews."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid


DEFAULT_BOOK = {
    "title": "由 AI 决定", "genre": "由 AI 决定", "premise": "由 AI 决定",
    "audience": "成人大众读者", "style": "自然、具体，有场景和人物行动",
    "ending": "完整收束主冲突，具体结局由 AI 决定",
    "chapter_count": 40, "target_chinese_chars": 100000,
    "chapter_min_chars": 1800, "length_tolerance": 0.1, "max_revision_rounds": 2,
}
CHAPTER_CHECKS = (
    "continuity", "causality", "character_knowledge", "plot_progress",
    "style", "repetition", "foreshadowing",
)
FINAL_CHECKS = ("completeness", "climax", "character_arcs", "foreshadowing", "continuity")
PLAN_FILES = ("plans/story-bible.md", "plans/ending.md", "plans/outline.json")
OUTPUT_FILES = ("exports/全书.md", "exports/全书.txt", "exports/验收报告.json")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U000323af]")
PLACEHOLDER = re.compile(
    r"(?im)^\s*(?:TODO|TBD|待补充|正文略|后续略|此处展开)(?:\s|[:：]|$)|"
    r"[\[【（(](?:待补充|待续写|正文略|此处省略|此处展开)[\]】）)]"
)


class WorkflowError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def metrics(text):
    body = "\n".join(line for line in text.splitlines() if not re.match(r"^\s*#{1,6}\s", line))
    return {
        "chinese_characters": len(CJK.findall(body)),
        "non_whitespace_characters": len(re.sub(r"\s", "", body)),
    }


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


class Novel:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()

    def path(self, relative):
        p = self.root / relative
        if not p.resolve().is_relative_to(self.root):
            raise WorkflowError(f"路径超出项目目录：{relative}")
        for part in [p, *p.parents]:
            if part == self.root:
                break
            if part.is_symlink():
                raise WorkflowError(f"工作流文件不允许使用符号链接：{relative}")
        return p

    def exists(self, relative):
        return self.path(relative).is_file()

    def read(self, relative):
        try:
            return self.path(relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as e:
            raise WorkflowError(f"无法读取 {relative}：{e}") from e

    def load(self, relative):
        try:
            return json.loads(self.read(relative))
        except json.JSONDecodeError as e:
            raise WorkflowError(f"JSON 格式错误 {relative}：第 {e.lineno} 行 {e.msg}") from e

    def sha(self, relative):
        try:
            return digest(self.path(relative).read_bytes())
        except OSError as e:
            raise WorkflowError(f"无法校验 {relative}：{e}") from e

    def write(self, relative, value):
        p = self.path(relative)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = value if isinstance(value, bytes) else value.encode("utf-8")
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=p.parent, prefix=".novel-", delete=False) as f:
                temp = Path(f.name)
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, p)
        finally:
            if temp and temp.exists():
                temp.unlink()

    def save(self, relative, value):
        self.write(relative, json_bytes(value))

    @contextmanager
    def lock(self):
        self.path("state").mkdir(parents=True, exist_ok=True)
        lock_path = self.path("state/workflow.lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as e:
            raise WorkflowError("其他命令正在修改进度，或上次进程异常退出。确认没有写作命令运行后再移除 state/workflow.lock；不要并发运行两个主创。") from e
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "created_at": now()}, f)
            yield
        finally:
            lock_path.unlink(missing_ok=True)

    def config(self):
        c = self.load("book.json")
        if not isinstance(c, dict):
            raise WorkflowError("book.json 必须是对象")
        for key in ("title", "genre", "premise", "audience", "style", "ending"):
            if not nonempty(c.get(key)):
                raise WorkflowError(f"book.json 缺少 {key}；不指定时填‘由 AI 决定’")
        for key in ("chapter_count", "target_chinese_chars", "chapter_min_chars"):
            if type(c.get(key)) is not int or c[key] < 1:
                raise WorkflowError(f"{key} 必须是正整数")
        tolerance = c.get("length_tolerance")
        if type(tolerance) not in (int, float) or not 0 <= tolerance <= 0.5:
            raise WorkflowError("length_tolerance 必须在 0～0.5 之间")
        if type(c.get("max_revision_rounds")) is not int or not 0 <= c["max_revision_rounds"] <= 2:
            raise WorkflowError("max_revision_rounds 必须是 0、1 或 2")
        if c["chapter_count"] * c["chapter_min_chars"] > c["target_chinese_chars"] * (1 + tolerance):
            raise WorkflowError("章节数×每章最短长度超过全书长度上限，请调整 book.json")
        return c

    def state(self):
        s = self.load("state/progress.json")
        if not isinstance(s, dict) or s.get("schema_version") != 1 or not isinstance(s.get("chapters"), dict):
            raise WorkflowError("进度格式不正确；不要手工重新初始化已有小说")
        if not isinstance(s.get("events"), list) or s.get("phase") not in ("planning", "writing", "final_review", "completed"):
            raise WorkflowError("进度阶段或事件记录无效，需从备份恢复")
        expected = {f"{i:03d}" for i in range(1, len(s["chapters"]) + 1)}
        if set(s["chapters"]) != expected:
            raise WorkflowError("进度中的章节不连续，需从备份恢复 state/progress.json")
        for entry in s["chapters"].values():
            if not isinstance(entry, dict) or not all(nonempty(entry.get(k)) for k in ("manuscript_sha256", "review_sha256", "memory_sha256")):
                raise WorkflowError("进度缺少章节校验摘要，需从备份恢复")
        return s

    def save_state(self, s):
        s["updated_at"] = now()
        self.save("state/progress.json", s)

    def init(self):
        with self.lock():
            if not self.exists("book.json"):
                self.save("book.json", DEFAULT_BOOK)
            self.config()
            for name in ("plans", "chapters", "memory", "reviews", "exports", "revisions"):
                self.path(name).mkdir(exist_ok=True)
            if not self.exists("创作任务书.md"):
                self.write("创作任务书.md", "# 创作任务书\n\n创作参数见 book.json。未指定的内容由 AI 决定，记录到 plans/story-bible.md。普通创作选择不必询问。\n")
            if not self.exists("state/progress.json"):
                self.save_state({"schema_version": 1, "phase": "planning", "plan_sha256": None,
                                 "chapters": {}, "events": [], "exports": {}})
            else:
                self.state()
        return {"ok": True, "root": str(self.root), "message": "初始化完成；已有文件未覆盖", "next_command": "python novel.py next"}

    def chapter_number(self, n):
        if n is None or not 1 <= n <= self.config()["chapter_count"]:
            raise WorkflowError("--chapter 必须在 1 到计划章节数之间")
        return f"{n:03d}"

    def plan_hash(self):
        return digest(json_bytes({p: self.sha(p) for p in ("book.json", "创作任务书.md", *PLAN_FILES)}))

    def constraints_hash(self):
        return digest(json_bytes({p: self.sha(p) for p in ("book.json", "创作任务书.md", *PLAN_FILES[:2])}))

    def context_hash(self, key):
        s = self.state()
        self.ready_plan(s)
        self.integrity(s)
        return digest(json_bytes({
            "constraints_sha256": self.constraints_hash(),
            "chapter_plan": self.outline()[int(key) - 1],
            "predecessors": {k: {field: v[field] for field in ("manuscript_sha256", "memory_sha256")}
                             for k, v in s["chapters"].items() if int(k) < int(key)},
        }))

    def outline(self):
        c = self.config()
        for p in PLAN_FILES[:2]:
            if len(self.read(p).strip()) < 40:
                raise WorkflowError(f"{p} 内容不足，请写出具体设定／结局")
        outline = self.load("plans/outline.json")
        if not isinstance(outline, list) or len(outline) != c["chapter_count"]:
            raise WorkflowError("outline.json 必须是数组，长度等于 chapter_count")
        for i, item in enumerate(outline, 1):
            if not isinstance(item, dict) or type(item.get("chapter")) is not int or item["chapter"] != i:
                raise WorkflowError("大纲章节必须从 1 开始连续排列")
            if not all(nonempty(item.get(k)) for k in ("title", "goal", "conflict", "change")):
                raise WorkflowError(f"大纲第 {i} 章必须有 title、goal、conflict、change")
        return outline

    def accept_plan(self, reason):
        with self.lock():
            s = self.state()
            outline = self.outline()
            if len(s["chapters"]) > len(outline):
                raise WorkflowError("计划章数小于已提交章数；先明确修订范围并 reopen")
            new_hash = self.plan_hash()
            if s["plan_sha256"] == new_hash:
                return {"ok": True, "message": "计划已接受，无变化"}
            if s["plan_sha256"] and not nonempty(reason):
                raise WorkflowError("修改已有计划需要 accept-plan --reason '说明影响与处理方式'")
            if s["chapters"] and s.get("constraints_sha256") != self.constraints_hash():
                raise WorkflowError("全局设定、任务书或配置变化会影响已提交章节；先 reopen --chapter 1 --reason '具体变更及复核计划'，再接受新计划")
            for key, entry in s["chapters"].items():
                if entry.get("outline_sha256") != digest(json_bytes(outline[int(key) - 1])):
                    raise WorkflowError(f"已提交第 {key} 章的大纲变化；先 reopen --chapter {int(key)} --reason '修订原因'")
            self.integrity(s)
            self.save("state/accepted-plan.json", {"plan_sha256": new_hash, "outline": outline})
            s["plan_sha256"] = new_hash
            s["constraints_sha256"] = self.constraints_hash()
            s["phase"] = "writing"
            s["exports"] = {}
            s["events"].append({"at": now(), "action": "accept-plan", "reason": reason or "首次规划"})
            self.save_state(s)
        return {"ok": True, "message": "计划已接受；继续执行 next"}

    def integrity(self, s):
        for key, entry in s["chapters"].items():
            for folder, ext, field in (("chapters", "md", "manuscript_sha256"), ("reviews", "json", "review_sha256"), ("memory", "json", "memory_sha256")):
                p = f"{folder}/{key}.{ext}"
                if not self.exists(p) or self.sha(p) != entry[field]:
                    raise WorkflowError(f"已提交文件缺失或被改动：{p}。核对后执行 reopen --chapter {int(key)} --reason '修订原因'，不要直接修改进度。")

    def ready_plan(self, s):
        self.outline()
        if not s["plan_sha256"] or s["plan_sha256"] != self.plan_hash():
            raise WorkflowError("计划、任务书或 book.json 尚未接受／已变化，请运行 accept-plan（已有计划需 --reason）")

    def draft_errors(self, key):
        p = f"chapters/{key}.md"
        if not self.exists(p):
            return [f"缺少 {p}"]
        text = self.read(p)
        errors = []
        if metrics(text)["chinese_characters"] < self.config()["chapter_min_chars"]:
            errors.append("正文未达到 chapter_min_chars；续写完整场景，不用重复内容凑字")
        if PLACEHOLDER.search(text):
            errors.append("正文含占位标记，不能用概要代替完整章节")
        return errors

    def review_errors(self, data, checks, hash_key, expected_hash, ready_key):
        if not isinstance(data, dict):
            return ["审稿必须是 JSON 对象"]
        errors = []
        if data.get(hash_key) != expected_hash:
            errors.append("审稿哈希与当前稿件不一致；使用 snapshot 获取摘要后重新审稿")
        if data.get(ready_key) is not True:
            errors.append(f"{ready_key} 尚未通过")
        rounds = data.get("revision_rounds", 0)
        if type(rounds) is not int or not 0 <= rounds <= self.config()["max_revision_rounds"]:
            errors.append("revision_rounds 超出约定范围")
        values = data.get("checks", {})
        for k in checks:
            v = values.get(k) if isinstance(values, dict) else None
            if not isinstance(v, dict) or v.get("result") != "pass" or not nonempty(v.get("evidence")):
                errors.append(f"检查项 {k} 需有 pass 结果和具体核验依据")
        issues = data.get("issues")
        if not isinstance(issues, list):
            errors.append("issues 必须是数组；无问题也需填 []")
        else:
            for item in issues:
                if (not isinstance(item, dict) or item.get("severity") not in ("critical", "major", "minor")
                        or type(item.get("resolved")) is not bool
                        or not all(nonempty(item.get(k)) for k in ("description", "evidence"))):
                    errors.append("问题记录格式不完整")
                elif item["severity"] in ("critical", "major") and not item["resolved"]:
                    errors.append("仍有未解决的严重问题：" + item["description"])
        return errors

    def chapter_review_errors(self, key):
        data = self.load(f"reviews/{key}.json")
        errors = self.review_errors(data, CHAPTER_CHECKS, "manuscript_sha256", self.sha(f"chapters/{key}.md"), "ready_for_commit")
        if not isinstance(data, dict) or type(data.get("chapter")) is not int or data["chapter"] != int(key):
            errors.append("审稿 chapter 与正文章号不符")
        if not isinstance(data, dict) or data.get("context_sha256") != self.context_hash(key):
            errors.append("审稿上下文已变化；需要结合最新前文和设定重新审稿")
        return errors

    def memory_errors(self, key):
        data = self.load(f"memory/{key}.json")
        text = self.read(f"chapters/{key}.md")
        if not isinstance(data, dict):
            return ["记忆必须是 JSON 对象"]
        errors = []
        if type(data.get("chapter")) is not int or data["chapter"] != int(key):
            errors.append("记忆 chapter 与正文章号不符")
        if data.get("manuscript_sha256") != self.sha(f"chapters/{key}.md"):
            errors.append("记忆哈希与最新正文不符，需要重新提取")
        if data.get("context_sha256") != self.context_hash(key):
            errors.append("记忆上下文已变化；需要复核前文变化对本章事实的影响")
        if not nonempty(data.get("summary")):
            errors.append("记忆缺少 summary")
        for kind, fields in (("facts", ("fact",)), ("characters", ("name", "state")), ("threads", ("id", "status", "note"))):
            items = data.get(kind)
            if not isinstance(items, list) or (kind == "facts" and not items):
                errors.append(f"{kind} 必须是数组，facts 至少需要一项")
                continue
            for item in items:
                if not isinstance(item, dict) or not all(nonempty(item.get(k)) for k in (*fields, "evidence")):
                    errors.append(f"{kind} 记录字段不完整")
                    continue
                if item["evidence"] not in text or len(item["evidence"].strip()) < 4:
                    errors.append(f"{kind} 的 evidence 必须引用本章至少4个字符的原文")
                if kind == "threads" and item["status"] not in ("open", "advanced", "resolved", "intentional_open"):
                    errors.append("伏笔 status 无效")
        return errors

    def validate_chapter(self, key):
        errors = self.draft_errors(key)
        if errors:
            raise WorkflowError("；".join(errors))
        for folder, validate in (("reviews", self.chapter_review_errors), ("memory", self.memory_errors)):
            if not self.exists(f"{folder}/{key}.json"):
                errors.append(f"缺少 {folder}/{key}.json")
            else:
                errors.extend(validate(key))
        if errors:
            raise WorkflowError("；".join(errors))

    def commit(self, n):
        key = self.chapter_number(n)
        with self.lock():
            s = self.state()
            self.ready_plan(s)
            self.integrity(s)
            if key in s["chapters"]:
                return {"ok": True, "message": f"第 {key} 章已经提交，无变化"}
            if n != len(s["chapters"]) + 1:
                raise WorkflowError("只能按顺序提交下一章，不能跳章")
            self.validate_chapter(key)
            entry = {
                "manuscript_sha256": self.sha(f"chapters/{key}.md"),
                "review_sha256": self.sha(f"reviews/{key}.json"),
                "memory_sha256": self.sha(f"memory/{key}.json"),
                "outline_sha256": digest(json_bytes(self.outline()[n - 1])),
                "context_sha256": self.context_hash(key),
                **metrics(self.read(f"chapters/{key}.md")), "committed_at": now(),
            }
            backup = f"revisions/commits/{key}-{uuid.uuid4().hex[:12]}"
            for folder, ext in (("chapters", "md"), ("reviews", "json"), ("memory", "json")):
                self.write(f"{backup}/{folder}/{key}.{ext}", self.path(f"{folder}/{key}.{ext}").read_bytes())
            s["chapters"][key] = entry
            s["phase"] = "final_review" if n == self.config()["chapter_count"] else "writing"
            s["exports"] = {}
            self.save_state(s)
        return {"ok": True, "chapter": n, **entry, "next_command": "python novel.py next"}

    def reopen(self, n, reason):
        key = self.chapter_number(n)
        if not nonempty(reason):
            raise WorkflowError("reopen 必须通过 --reason 说明修订范围和原因")
        with self.lock():
            s = self.state()
            if key not in s["chapters"]:
                raise WorkflowError("只能 reopen 已提交章节；未提交草稿可直接修订")
            backup = f"revisions/reopen-{uuid.uuid4().hex}"
            self.write(f"{backup}/progress.json", self.path("state/progress.json").read_bytes())
            affected = [k for k in s["chapters"] if int(k) >= n]
            for k in affected:
                for folder, ext in (("chapters", "md"), ("reviews", "json"), ("memory", "json")):
                    path = f"{folder}/{k}.{ext}"
                    if self.exists(path):
                        self.write(f"{backup}/{path}", self.path(path).read_bytes())
                        if folder != "chapters":
                            self.path(path).unlink()
                del s["chapters"][k]
            s["phase"] = "writing"
            s["exports"] = {}
            s["events"].append({"at": now(), "action": "reopen", "chapter": n, "reason": reason, "backup": backup})
            self.save_state(s)
        return {"ok": True, "invalidated_chapters": affected, "backup": backup,
                "message": "正文保留，旧审稿与记忆已归档到备份并失效。复核本章和后续依赖，重新生成审稿与记忆后按序提交；不必全部重写正文。"}

    def snapshot(self, n=None):
        if n is not None:
            key = self.chapter_number(n)
            return {"chapter": n, "manuscript_sha256": self.sha(f"chapters/{key}.md"),
                    "context_sha256": self.context_hash(key), **metrics(self.read(f"chapters/{key}.md"))}
        s = self.state()
        self.ready_plan(s)
        self.integrity(s)
        manifest = {"plan_sha256": s["plan_sha256"], "chapters": {
            k: {field: v[field] for field in ("manuscript_sha256", "review_sha256", "memory_sha256")}
            for k, v in s["chapters"].items()
        }}
        return {"snapshot_sha256": digest(json_bytes(manifest)), "committed_chapters": len(s["chapters"]),
                "chinese_characters": sum(v["chinese_characters"] for v in s["chapters"].values())}

    def review_template(self, key=None):
        if key:
            identity = {"chapter": int(key), "manuscript_sha256": self.sha(f"chapters/{key}.md"), "context_sha256": self.context_hash(key)}
            checks, ready = CHAPTER_CHECKS, "ready_for_commit"
        else:
            identity = {"snapshot_sha256": self.snapshot()["snapshot_sha256"]}
            checks, ready = FINAL_CHECKS, "ready_for_export"
        old_path = f"reviews/{key}.json" if key else "reviews/final.json"
        old = self.load(old_path) if self.exists(old_path) else {}
        rounds = old.get("revision_rounds", 0) if isinstance(old, dict) else 0
        rounds = rounds if type(rounds) is int and rounds >= 0 else 0
        return {**identity, "revision_rounds": rounds, "checks": {k: {"result": "pending", "evidence": ""} for k in checks},
                "issues": [], ready: False}

    def memory_template(self, key):
        return {"chapter": int(key), "manuscript_sha256": self.sha(f"chapters/{key}.md"), "context_sha256": self.context_hash(key), "summary": "",
                "facts": [{"fact": "", "evidence": ""}], "characters": [{"name": "", "state": "", "evidence": ""}],
                "threads": [{"id": "", "status": "open", "note": "", "evidence": ""}]}

    def packet(self, action, instructions, **extra):
        return {"action": action, "instructions": instructions,
                "loop": "执行本任务并落盘后继续运行 python novel.py next，直到 action=completed 或遇到真实阻塞。", **extra}

    def next(self):
        if not self.exists("state/progress.json"):
            return self.packet("initialize", "运行 init；不要仅向用户解释计划。", command="python novel.py init")
        c, s = self.config(), self.state()
        if not s["plan_sha256"]:
            return self.packet("plan", "读取任务书；自行决定未指定创作项。先确定结局，再写设定与逐章大纲。完成后 accept-plan，然后继续 next。",
                read=["book.json", "创作任务书.md", "START.md"], write=list(PLAN_FILES),
                outline_schema=[{"chapter": 1, "title": "章名", "goal": "人物目标", "conflict": "阻力", "change": "本章结尾产生的变化"}],
                chapter_count=c["chapter_count"], command="python novel.py accept-plan")
        self.integrity(s)
        if s["plan_sha256"] != self.plan_hash():
            return self.packet("review_plan_change", "核查任务书或计划变更对已有正文的影响，记录原因后重新 accept-plan。已提交章节大纲变化需先 reopen。",
                               command="python novel.py accept-plan --reason '填写实际变更及影响'")
        self.ready_plan(s)
        n = len(s["chapters"]) + 1
        if n <= c["chapter_count"]:
            key = f"{n:03d}"
            read = ["book.json", "创作任务书.md", *PLAN_FILES, "state/progress.json"]
            read += [f"chapters/{i:03d}.md" for i in range(max(1, n - 2), n)]
            read += [f"memory/{i:03d}.json" for i in range(1, n)]
            if self.exists(f"chapters/{key}.md"):
                read.append(f"chapters/{key}.md")
            context = {"chapter": n, "outline": self.outline()[n - 1], "read": read}
            if not self.exists(f"chapters/{key}.md") or self.draft_errors(key):
                return self.packet("write_chapter", "逐章写完整正文。已有草稿先读取并续写，不覆盖成梗概。按需查前文证据；默认每5章附加跨章审查。写完继续 next。",
                    **context, write=[f"chapters/{key}.md"], minimum_chinese_chars=c["chapter_min_chars"],
                    suggested_chinese_chars=round(c["target_chinese_chars"] / c["chapter_count"]),
                    problems=self.draft_errors(key))
            review = f"reviews/{key}.json"
            errors = self.chapter_review_errors(key) if self.exists(review) else ["尚未审稿"]
            if errors:
                old = self.load(review) if self.exists(review) else {}
                rounds = old.get("revision_rounds", 0) if isinstance(old, dict) else 0
                exhausted = (isinstance(old, dict) and type(rounds) is int and rounds >= c["max_revision_rounds"]
                             and old.get("manuscript_sha256") == self.sha(f"chapters/{key}.md")
                             and old.get("context_sha256") == self.context_hash(key))
                return self.packet("blocked" if exhausted else "review_chapter",
                    "审阅本章及相关前文，写出具体证据；真实存在的问题先修正文，再用新哈希重新审稿，累计修订轮数。不得仅把结果改为pass；用完修订轮数仍无法通过则保存问题并停止。",
                    **context, problems=errors, write=[review], template=self.review_template(key))
            memory = f"memory/{key}.json"
            errors = self.memory_errors(key) if self.exists(memory) else ["尚未提取本章事实"]
            if errors:
                return self.packet("update_memory", "从最终正文提取事实。evidence为本章原文片段；人物状态和伏笔注明变化，不把未来大纲写成事实。无人物或伏笔变化时对应数组可为[]。",
                    **context, problems=errors, write=[memory], template=self.memory_template(key))
            return self.packet("commit_chapter", "正文、审稿、记忆已通过结构校验；执行提交，再继续 next。", **context,
                               command=f"python novel.py commit --chapter {n}")
        snap = self.snapshot()
        lower, upper = c["target_chinese_chars"] * (1 - c["length_tolerance"]), c["target_chinese_chars"] * (1 + c["length_tolerance"])
        if not lower <= snap["chinese_characters"] <= upper:
            return self.packet("revise_length", "实际长度不在任务约定范围。找出需要增删的具体场景，从最早受影响章reopen后修订、复核并按序提交。不要灌水或偷偷降低目标。",
                               **snap, expected_range=[lower, upper])
        final = "reviews/final.json"
        errors = self.review_errors(self.load(final), FINAL_CHECKS, "snapshot_sha256", snap["snapshot_sha256"], "ready_for_export") if self.exists(final) else ["尚未进行全书审查"]
        if errors:
            old = self.load(final) if self.exists(final) else {}
            rounds = old.get("revision_rounds", 0) if isinstance(old, dict) else 0
            exhausted = (isinstance(old, dict) and type(rounds) is int and rounds >= c["max_revision_rounds"]
                         and old.get("snapshot_sha256") == snap["snapshot_sha256"])
            return self.packet("blocked" if exhausted else "final_review", "核查全书主冲突、人物弧线、关键伏笔和连续性；根据台账回读原文。发现严重问题需reopen最早受影响章修订，不能虚报通过。最终审查最多两轮，仍有严重问题则保存报告并停止。",
                problems=errors, read=["book.json", "创作任务书.md", *PLAN_FILES]
                + [f"memory/{i:03d}.json" for i in range(1, c["chapter_count"] + 1)]
                + [f"chapters/{i:03d}.md" for i in range(1, c["chapter_count"] + 1)],
                write=[final], template=self.review_template(), **snap)
        if (s["phase"] == "completed" and s.get("final_review_sha256") == self.sha(final)
                and all(self.exists(p) and self.sha(p) == s.get("exports", {}).get(p) for p in OUTPUT_FILES)):
            return {"action": "completed", "message": "全书已验收并导出。结构校验不代表文学质量保证。", **snap, "files": list(OUTPUT_FILES)}
        return self.packet("export", "审稿和长度已通过；执行 export 完成合稿。", command="python novel.py export", **snap)

    def check(self, n=None):
        self.config()
        s = self.state()
        self.integrity(s)
        if s["plan_sha256"]:
            self.ready_plan(s)
        if n is not None:
            self.ready_plan(s)
            self.validate_chapter(self.chapter_number(n))
        if s["phase"] == "completed":
            if not self.exists("reviews/final.json") or self.sha("reviews/final.json") != s.get("final_review_sha256"):
                raise WorkflowError("最终审稿已变化或缺失，请重新验收和导出")
            for p in OUTPUT_FILES:
                if not self.exists(p) or self.sha(p) != s.get("exports", {}).get(p):
                    raise WorkflowError(f"导出文件缺失或已变化：{p}；核对后重新 export")
        return {"ok": True, "committed_chapters": len(s["chapters"]), "message": "文件、结构与哈希检查通过；语义审稿由 Agent 负责。"}

    def status(self):
        c = self.config()
        s = self.state()
        problems = []
        try:
            self.integrity(s)
            if s["plan_sha256"]:
                self.ready_plan(s)
        except WorkflowError as e:
            problems.append(str(e))
        return {"phase": s["phase"], "planned_chapters": c["chapter_count"], "committed_chapters": len(s["chapters"]),
                "chinese_characters": sum(x.get("chinese_characters", 0) for x in s["chapters"].values()),
                "integrity_ok": not problems, "problems": problems, "next_command": "python novel.py next"}

    def export(self):
        with self.lock():
            c, s = self.config(), self.state()
            self.ready_plan(s)
            self.integrity(s)
            if len(s["chapters"]) != c["chapter_count"]:
                raise WorkflowError("仍有章节未提交，不能导出为完结作品")
            snap = self.snapshot()
            if abs(snap["chinese_characters"] - c["target_chinese_chars"]) > c["target_chinese_chars"] * c["length_tolerance"]:
                raise WorkflowError("全书实际字数超出任务书范围")
            final = self.load("reviews/final.json")
            errors = self.review_errors(final, FINAL_CHECKS, "snapshot_sha256", snap["snapshot_sha256"], "ready_for_export")
            if errors:
                raise WorkflowError("；".join(errors))
            parts = [self.read(f"chapters/{i:03d}.md").strip() for i in range(1, c["chapter_count"] + 1)]
            title = c["title"] if c["title"] != "由 AI 决定" else "全书"
            manuscript = f"# {title}\n\n" + "\n\n".join(parts) + "\n"
            plain = re.sub(r"(?m)^\s*#{1,6}\s+", "", manuscript)
            self.write(OUTPUT_FILES[0], manuscript)
            self.write(OUTPUT_FILES[1], plain)
            self.save(OUTPUT_FILES[2], {"exported_at": now(), **snap, "chapters": c["chapter_count"],
                "mechanical_checks": "passed", "agent_review": final,
                "note": "哈希与结构检查不能替代文学质量、事实和版权的最终人工判断。"})
            s["exports"] = {p: self.sha(p) for p in OUTPUT_FILES}
            s["final_review_sha256"] = self.sha("reviews/final.json")
            s["phase"] = "completed"
            self.save_state(s)
        return {"ok": True, "action": "completed", **snap, "files": list(OUTPUT_FILES)}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent), help="小说项目目录（默认脚本所在目录）")
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "next", "status", "export"):
        subs.add_parser(name)
    for name in ("snapshot", "check", "commit", "reopen"):
        p = subs.add_parser(name)
        p.add_argument("--chapter", type=int, required=name in ("commit", "reopen"))
        if name == "reopen":
            p.add_argument("--reason", required=True)
    p = subs.add_parser("accept-plan")
    p.add_argument("--reason")
    args = parser.parse_args(argv)
    app = Novel(args.root)
    try:
        if args.command == "accept-plan":
            result = app.accept_plan(args.reason)
        elif args.command == "reopen":
            result = app.reopen(args.chapter, args.reason)
        elif args.command in ("snapshot", "check", "commit"):
            result = getattr(app, args.command)(args.chapter)
        else:
            result = getattr(app, args.command)()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (WorkflowError, OSError, ValueError) as e:
        print(json.dumps({"ok": False, "action": "blocked", "error": str(e)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
