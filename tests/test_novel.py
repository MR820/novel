"""End-to-end checks for the file-based novel workflow (no model/API calls)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "novel.py"
CHAPTER_CHECKS = (
    "continuity", "causality", "character_knowledge", "plot_progress",
    "style", "repetition", "foreshadowing",
)
FINAL_CHECKS = (
    "completeness", "climax", "character_arcs", "foreshadowing", "continuity",
)


class NovelWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="novel-workflow-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "book"
        self.call("init")

    def call(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *map(str, args)],
            text=True, capture_output=True, encoding="utf-8", timeout=20,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        path.write_text(value, encoding="utf-8")
        return path

    def read_json(self, relative):
        return json.loads((self.root / relative).read_text(encoding="utf-8"))

    @staticmethod
    def chapter_body(number):
        return (
            f"第{number}段旅途中，林岚在桥边找到母亲留下的铜钥匙。"
            f"她核对第{number}封信上的日期，决定沿着河岸寻找旧车站。"
            "这一次她没有逃避，而是向同行的朋友说明了自己的打算。"
            "两人在日落前穿过石门，把钥匙交给守门人，终于打开了归家的路。"
        )

    def prepare_plan(self, count=3, accept=True):
        config = self.read_json("book.json")
        total = sum(len(re.findall(r"[\u4e00-\u9fff]", self.chapter_body(n)))
                    for n in range(1, count + 1))
        config.update({
            "title": "归途", "genre": "悬疑", "premise": "林岚寻找母亲留下的归家线索。",
            "audience": "成人读者", "style": "克制具体", "ending": "林岚回家并解开误会。",
            "chapter_count": count, "target_chinese_chars": total,
            "chapter_min_chars": 10, "length_tolerance": 0.2,
            "max_revision_rounds": 2,
        })
        self.write("book.json", config)
        self.write("plans/story-bible.md", "# 故事设定\n\n林岚为了找到母亲留下的信，回到童年生活的小城。"
                   "她只知道信封上的地址，尚不知道铜钥匙的用途。故事按自然时间推进，人物没有超自然能力。\n")
        self.write("plans/ending.md", "# 结局\n\n林岚终于打开老屋，发现母亲一直保存着她的来信。"
                   "她理解了当年的离别，与家人重新建立联系，主线在归家后完整结束。\n")
        self.write("plans/outline.json", [
            {"chapter": n, "title": f"归途{n}", "goal": "寻找归家线索",
             "conflict": "旧日误会阻止她开口", "change": f"第{n}次前进让她获得新的线索"}
            for n in range(1, count + 1)
        ])
        if accept:
            self.call("accept-plan")

    def prepare_chapter(self, number, *, issues=None, revision_rounds=0):
        body = self.chapter_body(number)
        path = self.write(f"chapters/{number:03d}.md", f"# 第{number}章 归途\n\n{body}\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot = json.loads(self.call("snapshot", "--chapter", number).stdout)
        context = {"context_sha256": snapshot["context_sha256"]} if "context_sha256" in snapshot else {}
        self.write(f"reviews/{number:03d}.json", {
            "chapter": number, "manuscript_sha256": digest,
            **context,
            "revision_rounds": revision_rounds,
            "checks": {k: {"result": "pass", "evidence": body} for k in CHAPTER_CHECKS},
            "issues": issues or [], "ready_for_commit": True,
        })
        self.write(f"memory/{number:03d}.json", {
            "chapter": number, "manuscript_sha256": digest,
            **context,
            "summary": "林岚沿着线索寻找归途，并与朋友交流自己的打算。",
            "facts": [{"fact": "林岚找到了铜钥匙", "evidence": "林岚在桥边找到母亲留下的铜钥匙"}],
            "characters": [{"name": "林岚", "state": "找到铜钥匙并与朋友同行", "evidence": "林岚在桥边找到母亲留下的铜钥匙"}],
            "threads": [],
        })
        return path

    def finish_chapters(self, count):
        self.prepare_plan(count)
        for number in range(1, count + 1):
            self.prepare_chapter(number)
            self.call("commit", "--chapter", number)

    def final_review(self):
        output = json.loads(self.call("snapshot").stdout)
        digest = output["snapshot_sha256"]
        self.write("reviews/final.json", {
            "snapshot_sha256": digest,
            "checks": {k: {"result": "pass", "evidence": "逐章核对正文和既定结局，主线已在最后一章收束。"}
                       for k in FINAL_CHECKS},
            "issues": [], "ready_for_export": True,
        })

    def test_init_is_idempotent_and_preserves_user_files(self):
        config = self.read_json("book.json")
        config["title"] = "用户已经选定的书名"
        self.write("book.json", config)
        self.write("chapters/001.md", "用户写了一半的正文，不可覆盖。")
        self.call("init")
        self.assertEqual(self.read_json("book.json")["title"], config["title"])
        self.assertEqual((self.root / "chapters/001.md").read_text(encoding="utf-8"), "用户写了一半的正文，不可覆盖。")

    def test_plan_rejects_incomplete_outline(self):
        self.prepare_plan(3, accept=False)
        outline = self.read_json("plans/outline.json")
        self.write("plans/outline.json", outline[:-1])
        self.call("accept-plan", success=False)

    def test_plan_rejects_missing_ending(self):
        self.prepare_plan(3, accept=False)
        (self.root / "plans/ending.md").unlink()
        self.call("accept-plan", success=False)

    def test_changed_plan_requires_reason(self):
        self.prepare_plan(3)
        outline = self.read_json("plans/outline.json")
        outline[0]["goal"] = "先找到关键目击者，再寻找旧屋"
        self.write("plans/outline.json", outline)
        self.call("accept-plan", success=False)
        self.call("accept-plan", "--reason", "调整第一章调查顺序")

    def test_global_setting_change_requires_reopening_existing_chapters(self):
        self.prepare_plan(3)
        self.prepare_chapter(1)
        self.call("commit", "--chapter", 1)
        bible = self.root / "plans/story-bible.md"
        bible.write_text(bible.read_text(encoding="utf-8") + "林岚从第一章起已经知道钥匙的全部用途。\n", encoding="utf-8")
        self.call("accept-plan", "--reason", "修改主角初始知识", success=False)

    def test_future_outline_can_change_without_invalidating_earlier_chapters(self):
        self.prepare_plan(3)
        self.prepare_chapter(1)
        self.call("commit", "--chapter", 1)
        outline = self.read_json("plans/outline.json")
        outline[2]["goal"] = "让林岚主动修复她与家人的关系"
        self.write("plans/outline.json", outline)
        self.call("accept-plan", "--reason", "细化尚未写作的第三章")
        self.call("check")
        self.prepare_chapter(2)
        self.call("commit", "--chapter", 2)

    def test_commit_rejects_missing_review(self):
        self.prepare_plan()
        self.prepare_chapter(1)
        (self.root / "reviews/001.json").unlink()
        self.call("commit", "--chapter", 1, success=False)

    def test_commit_rejects_stale_review(self):
        self.prepare_plan()
        path = self.prepare_chapter(1)
        path.write_text(path.read_text(encoding="utf-8") + "她随后发现了新的信件。\n", encoding="utf-8")
        self.call("commit", "--chapter", 1, success=False)

    def test_commit_rejects_stale_memory(self):
        self.prepare_plan()
        self.prepare_chapter(1)
        memory = self.read_json("memory/001.json")
        memory["manuscript_sha256"] = "0" * 64
        self.write("memory/001.json", memory)
        self.call("commit", "--chapter", 1, success=False)

    def test_commit_rejects_stale_context(self):
        self.prepare_plan()
        self.prepare_chapter(1)
        review = self.read_json("reviews/001.json")
        review["context_sha256"] = "0" * 64
        self.write("reviews/001.json", review)
        self.call("commit", "--chapter", 1, success=False)

    def test_upstream_revision_invalidates_unchanged_downstream_review(self):
        self.finish_chapters(2)
        old_review = self.read_json("reviews/002.json")
        old_memory = self.read_json("memory/002.json")
        self.call("reopen", "--chapter", 1, "--reason", "第一章增加改变后续理解的发现")
        first = self.prepare_chapter(1)
        first.write_text(first.read_text(encoding="utf-8") + "林岚在临走前发现，信封上还有另一人的姓名。\n", encoding="utf-8")
        digest = hashlib.sha256(first.read_bytes()).hexdigest()
        for relative in ["reviews/001.json", "memory/001.json"]:
            data = self.read_json(relative)
            data["manuscript_sha256"] = digest
            self.write(relative, data)
        self.call("commit", "--chapter", 1)
        new_context = json.loads(self.call("snapshot", "--chapter", 2).stdout)["context_sha256"]
        self.assertNotEqual(old_review["context_sha256"], new_context)
        self.write("reviews/002.json", old_review)
        self.write("memory/002.json", old_memory)
        self.call("commit", "--chapter", 2, success=False)

    def test_commit_rejects_invented_fact_evidence(self):
        self.prepare_plan()
        self.prepare_chapter(1)
        memory = self.read_json("memory/001.json")
        memory["facts"][0]["evidence"] = "正文中从未出现的未来情节证据"
        self.write("memory/001.json", memory)
        self.call("commit", "--chapter", 1, success=False)

    def test_commit_rejects_unresolved_serious_issue(self):
        self.prepare_plan()
        self.prepare_chapter(1, issues=[{
            "severity": "critical", "description": "人物提前知道结局秘密",
            "evidence": "她核对第1封信上的日期", "resolved": False,
        }])
        self.call("commit", "--chapter", 1, success=False)

    def test_commit_rejects_failed_review_check(self):
        self.prepare_plan()
        self.prepare_chapter(1)
        review = self.read_json("reviews/001.json")
        review["checks"]["continuity"]["result"] = "fail"
        self.write("reviews/001.json", review)
        self.call("commit", "--chapter", 1, success=False)

    def test_commit_rejects_skipping_chapter(self):
        self.prepare_plan()
        self.prepare_chapter(2)
        self.call("commit", "--chapter", 2, success=False)

    def test_commit_is_idempotent_across_processes(self):
        self.prepare_plan()
        path = self.prepare_chapter(1)
        original = path.read_bytes()
        self.call("commit", "--chapter", 1)
        self.call("commit", "--chapter", 1)
        self.call("status")
        self.call("next")
        self.assertEqual(path.read_bytes(), original)
        self.prepare_chapter(2)
        self.call("commit", "--chapter", 2)

    def test_committed_text_tampering_is_detected(self):
        self.prepare_plan()
        path = self.prepare_chapter(1)
        self.call("commit", "--chapter", 1)
        path.write_text(path.read_text(encoding="utf-8") + "这段未经审稿的正文改变了剧情。\n", encoding="utf-8")
        self.call("check", success=False)
        self.call("commit", "--chapter", 1, success=False)

    def test_missing_committed_chapter_is_detected(self):
        self.prepare_plan()
        path = self.prepare_chapter(1)
        self.call("commit", "--chapter", 1)
        path.unlink()
        self.call("check", success=False)

    def test_incomplete_manuscript_cannot_export(self):
        self.prepare_plan(3)
        self.prepare_chapter(1)
        self.call("commit", "--chapter", 1)
        self.call("export", success=False)

    def test_complete_manuscript_without_final_review_cannot_export(self):
        self.finish_chapters(2)
        self.call("export", success=False)

    def test_export_in_numeric_order_and_idempotent(self):
        self.finish_chapters(12)
        self.final_review()
        self.call("export")
        exports = [p for p in (self.root / "exports").iterdir() if p.suffix in {".md", ".txt"}]
        self.assertGreaterEqual(len(exports), 2)
        for path in exports:
            text = path.read_text(encoding="utf-8")
            positions = [text.index(f"第{n}段旅途中") for n in range(1, 13)]
            self.assertEqual(positions, sorted(positions))
            for number in range(1, 13):
                self.assertEqual(text.count(f"第{number}段旅途中"), 1)
        before = {p.name: p.read_bytes() for p in exports}
        self.call("export")
        self.assertEqual(before, {p.name: p.read_bytes() for p in exports})
        self.assertEqual(json.loads(self.call("next").stdout)["action"], "completed")

    def test_completed_export_tampering_is_detected(self):
        self.finish_chapters(1)
        self.final_review()
        self.call("export")
        export = next((self.root / "exports").glob("*.md"))
        export.write_text(export.read_text(encoding="utf-8") + "未经复核的改动。", encoding="utf-8")
        self.call("check", success=False)

    def test_completed_final_review_tampering_is_detected(self):
        self.finish_chapters(1)
        self.final_review()
        self.call("export")
        review = self.read_json("reviews/final.json")
        review["ready_for_export"] = False
        self.write("reviews/final.json", review)
        self.call("check", success=False)

    def test_final_review_hash_must_match_snapshot(self):
        self.finish_chapters(2)
        self.final_review()
        review = self.read_json("reviews/final.json")
        review["snapshot_sha256"] = "f" * 64
        self.write("reviews/final.json", review)
        self.call("export", success=False)

    def test_reopen_requires_reason_and_preserves_drafts(self):
        self.finish_chapters(3)
        originals = {p.name: p.read_bytes() for p in (self.root / "chapters").glob("*.md")}
        self.call("reopen", "--chapter", 2, success=False)
        self.call("reopen", "--chapter", 2, "--reason", "修订第二章的人物动机")
        self.assertEqual(originals, {p.name: p.read_bytes() for p in (self.root / "chapters").glob("*.md")})
        for number in [2, 3]:
            self.assertFalse((self.root / f"reviews/{number:03d}.json").exists())
            self.assertFalse((self.root / f"memory/{number:03d}.json").exists())
        self.call("commit", "--chapter", 3, success=False)
        self.call("export", success=False)
        self.prepare_chapter(2)
        self.call("commit", "--chapter", 2)
        self.prepare_chapter(3)
        self.call("commit", "--chapter", 3)

    def test_final_review_stops_at_revision_limit(self):
        self.finish_chapters(2)
        self.final_review()
        review = self.read_json("reviews/final.json")
        review["revision_rounds"] = 2
        review["checks"]["continuity"]["result"] = "fail"
        review["ready_for_export"] = False
        self.write("reviews/final.json", review)
        result = json.loads(self.call("next").stdout)
        self.assertEqual(result["action"], "blocked")

    def test_review_template_preserves_revision_count(self):
        self.prepare_plan()
        self.prepare_chapter(1, revision_rounds=1)
        review = self.read_json("reviews/001.json")
        review["checks"]["continuity"]["result"] = "fail"
        review["ready_for_commit"] = False
        self.write("reviews/001.json", review)
        result = json.loads(self.call("next").stdout)
        self.assertEqual(result["action"], "review_chapter")
        self.assertEqual(result["template"]["revision_rounds"], 1)

    def test_snapshot_counts_body_not_markdown_headings(self):
        self.prepare_plan(1)
        path = self.prepare_chapter(1)
        path.write_text("# 不应统计这段标题\n\n中文ABC123，空 格。\n", encoding="utf-8")
        result = json.loads(self.call("snapshot", "--chapter", 1).stdout)
        self.assertEqual(result["chinese_characters"], 4)
        self.assertEqual(result["manuscript_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_length_mismatch_blocks_export(self):
        self.prepare_plan(2, accept=False)
        config = self.read_json("book.json")
        config["target_chinese_chars"] *= 3
        self.write("book.json", config)
        self.call("accept-plan")
        for number in [1, 2]:
            self.prepare_chapter(number)
            self.call("commit", "--chapter", number)
        self.final_review()
        result = json.loads(self.call("next").stdout)
        self.assertEqual(result["action"], "revise_length")
        self.call("export", success=False)

    def test_committed_memory_tampering_is_detected(self):
        self.prepare_plan()
        self.prepare_chapter(1)
        self.call("commit", "--chapter", 1)
        memory = self.read_json("memory/001.json")
        memory["summary"] = "有人未经复核就改变了本章人物状态。"
        self.write("memory/001.json", memory)
        self.call("check", success=False)
        self.call("snapshot", success=False)

    def test_existing_mutation_lock_is_preserved(self):
        before = (self.root / "state/progress.json").read_bytes()
        lock = self.write("state/workflow.lock", {"pid": 99999999, "created_at": "2020-01-01T00:00:00Z"})
        original_lock = lock.read_bytes()
        self.call("init", success=False)
        self.assertEqual(lock.read_bytes(), original_lock)
        self.assertEqual((self.root / "state/progress.json").read_bytes(), before)

    def test_next_preserves_incomplete_draft(self):
        self.prepare_plan(2)
        path = self.write("chapters/001.md", "# 第一章\n\n未完成。")
        before = path.read_bytes()
        result = json.loads(self.call("next").stdout)
        self.assertEqual(result["action"], "write_chapter")
        self.assertEqual(path.read_bytes(), before)

    def test_next_routes_full_chapter_workflow_with_current_prose(self):
        self.prepare_plan(1)
        self.assertEqual(json.loads(self.call("next").stdout)["action"], "write_chapter")
        self.prepare_chapter(1)
        review = self.read_json("reviews/001.json")
        memory = self.read_json("memory/001.json")
        (self.root / "reviews/001.json").unlink()
        (self.root / "memory/001.json").unlink()
        packet = json.loads(self.call("next").stdout)
        self.assertEqual(packet["action"], "review_chapter")
        self.assertIn("chapters/001.md", packet["read"])
        self.write("reviews/001.json", review)
        packet = json.loads(self.call("next").stdout)
        self.assertEqual(packet["action"], "update_memory")
        self.assertIn("chapters/001.md", packet["read"])
        self.write("memory/001.json", memory)
        self.assertEqual(json.loads(self.call("next").stdout)["action"], "commit_chapter")
        self.call("commit", "--chapter", 1)
        self.assertEqual(json.loads(self.call("next").stdout)["action"], "final_review")
        self.final_review()
        self.assertEqual(json.loads(self.call("next").stdout)["action"], "export")
        self.call("export")
        self.assertEqual(json.loads(self.call("next").stdout)["action"], "completed")

    def test_invalid_chapter_numbers_are_rejected(self):
        self.prepare_plan(3)
        for number in [0, -1, 4]:
            with self.subTest(number=number):
                self.call("commit", "--chapter", number, success=False)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_external_chapter_symlink_is_not_followed(self):
        self.prepare_plan(1)
        path = self.prepare_chapter(1)
        outside = Path(self.temp.name) / "private-original.md"
        outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("symlink creation not permitted")
        before = outside.read_bytes()
        self.call("commit", "--chapter", 1, success=False)
        self.assertEqual(before, outside.read_bytes())


if __name__ == "__main__":
    unittest.main()
