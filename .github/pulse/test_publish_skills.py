"""Tests for publish_skills.py. Run: python -I .github/pulse/test_publish_skills.py (stdlib only)."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_skills as ps  # noqa: E402

SUMMARY = "Does a useful thing for the team"

TEMPLATE_SNIPPET = [
    "# internal = Open DealFlow staff agents only; shareable = every customer agent.",
    "# metadata:",
    "#   pulse: internal",
    "#   pulse-summary: One line, 10 to 160 characters, saying what the skill is for.",
]


def fm(name="odf-demo", pulse="shareable", summary=SUMMARY, nl="\n", bom=""):
    lines = ["---", f"name: {name}", "description: >-", "  first folded line", "  second folded line", "  third folded line"]
    if pulse is not None:
        lines += ["metadata:", f"  pulse: {pulse}"]
        if summary is not None:
            lines.append(f"  pulse-summary: {summary}")
    lines.append("---")
    return bom + nl.join(lines) + nl


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
        return p


class ParseTests(Base):
    def test_folded_description_skipped_and_metadata_read(self):
        top, meta, body = ps.parse_frontmatter(fm() + "# Body\n")
        self.assertEqual(top["name"], "odf-demo")
        self.assertEqual(meta["pulse"], "shareable")
        self.assertEqual(meta["pulse-summary"], SUMMARY)
        self.assertEqual(body.strip(), "# Body")
        self.assertNotIn("first folded line", json.dumps(top))

    def test_crlf_and_bom(self):
        top, meta, body = ps.parse_frontmatter(fm(nl="\r\n", bom="\ufeff") + "# Body\r\n")
        self.assertEqual(top["name"], "odf-demo")
        self.assertEqual(meta["pulse"], "shareable")
        self.assertEqual(body.strip(), "# Body")

    def test_no_metadata(self):
        top, meta, _ = ps.parse_frontmatter(fm(pulse=None) + "x\n")
        self.assertEqual(meta, {})
        self.assertEqual(top["name"], "odf-demo")

    def test_literal_block_children_are_not_metadata(self):
        text = "---\nname: odf-x\nnotes: |\n  pulse: shareable\n---\nbody\n"
        top, meta, _ = ps.parse_frontmatter(text)
        self.assertEqual(meta, {})

    def test_no_frontmatter(self):
        top, meta, body = ps.parse_frontmatter("# just text\n")
        self.assertEqual((top, meta, body), ({}, {}, "# just text\n"))


class BuildTests(Base):
    def test_unmarked_is_none(self):
        p = self.write("a/SKILL.md", fm(pulse=None) + "body\n")
        self.assertIsNone(ps.build_skill(p, self.root))

    def test_invalid_audience_fails_loudly(self):
        for bad in ("public", "", "internal-ish"):
            p = self.write("a/SKILL.md", fm(pulse=bad) + "body\n")
            with self.assertRaises(ps.SkillError, msg=repr(bad)) as c:
                ps.build_skill(p, self.root)
            self.assertIn("a/SKILL.md", str(c.exception))

    def test_inline_comment_tail_stripped(self):
        p = self.write("a/SKILL.md", fm(pulse="internal   # staff only", summary="Does a useful thing # note") + "body\n")
        s = ps.build_skill(p, self.root)
        self.assertEqual(s["audience"], "internal")
        self.assertEqual(s["summary"], "Does a useful thing")
        p = self.write("a/SKILL.md", fm(summary='"Keeps a # inside quotes ok" # tail') + "body\n")
        self.assertEqual(ps.build_skill(p, self.root)["summary"], "Keeps a # inside quotes ok")

    def test_template_example_when_uncommented_publishes(self):
        # Self-contained: the snippet below is copied from _template/SKILL.md, so this passes in any skill repo.
        lines = [l[2:] if l.startswith("# metadata:") or l.startswith("#   pulse") else l for l in TEMPLATE_SNIPPET]
        text = "\n".join(["---", "name: odf-tpl", "description: A demo skill"] + lines + ["---", "", "Body text.", ""])
        p = self.write("a/SKILL.md", text)
        s = ps.build_skill(p, self.root)
        self.assertEqual(s["audience"], "internal")
        self.assertEqual(s["summary"], "One line, 10 to 160 characters, saying what the skill is for.")

    def test_template_snippet_matches_template_skill_md(self):
        # Only meaningful inside odf-skills-meta/_template; skipped in a skill repo (its SKILL.md has no marker).
        f = Path(__file__).resolve().parents[2] / "SKILL.md"
        if not f.is_file() or "# metadata:" not in f.read_text(encoding="utf-8"):
            self.skipTest("not the template")
        have = f.read_text(encoding="utf-8").splitlines()
        for line in TEMPLATE_SNIPPET:
            self.assertIn(line, have)

    def test_symlink_outside_repo_is_a_clear_error(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "SKILL.md"
        target.write_bytes((fm() + "body\n").encode("utf-8"))
        (self.root / "a").mkdir()
        try:
            os.symlink(target, self.root / "a" / "SKILL.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted here")
        with self.assertRaises(ps.SkillError) as c:
            ps.build_skill(self.root / "a" / "SKILL.md", self.root)
        self.assertIn("outside the repo", str(c.exception))

    def test_symlinked_reference_outside_repo_is_a_clear_error(self):
        p = self.write("a/SKILL.md", fm() + "main\n")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        target = Path(outside.name) / "r.md"
        target.write_bytes(b"secret-ish\n")
        (self.root / "a" / "references").mkdir()
        try:
            os.symlink(target, self.root / "a" / "references" / "r.md")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not permitted here")
        with self.assertRaises(ps.SkillError) as c:
            ps.build_skill(p, self.root)
        self.assertIn("outside the repo", str(c.exception))

    def test_file_outside_repo_is_a_clear_error_without_symlinks(self):
        # Same path a symlink escape takes (resolve() lands outside the repo), testable where symlinks are not allowed.
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        f = Path(outside.name) / "SKILL.md"
        f.write_bytes((fm() + "body\n").encode("utf-8"))
        with self.assertRaises(ps.SkillError) as c:
            ps.build_skill(f, self.root)
        self.assertIn("outside the repo", str(c.exception))
        with self.assertRaises(ps.SkillError):
            ps._rel(f, self.root)

    def test_workflow_publish_step_only_on_main(self):
        wf_path = Path(__file__).resolve().parents[1] / "workflows" / "publish-to-pulse.yml"
        if not wf_path.is_file():
            self.skipTest("workflow not beside the tests")
        wf = wf_path.read_text(encoding="utf-8")
        step = wf[wf.index("- name: Publish to Pulse"):]
        self.assertIn("if: github.ref == 'refs/heads/main'", step)

    def test_case_insensitive_audience_and_quoted_summary(self):
        p = self.write("a/SKILL.md", fm(pulse="Shareable", summary='"Quoted summary line here"') + "body\n")
        s = ps.build_skill(p, self.root)
        self.assertEqual(s["audience"], "shareable")
        self.assertEqual(s["summary"], "Quoted summary line here")
        self.assertEqual(s["path"], "a/SKILL.md")
        self.assertEqual(s["name"], "odf-demo")

    def test_marked_without_summary_errors_with_path(self):
        p = self.write("a/SKILL.md", fm(summary=None) + "body\n")
        with self.assertRaises(ps.SkillError) as c:
            ps.build_skill(p, self.root)
        self.assertIn("a/SKILL.md", str(c.exception))

    def test_summary_length_rules(self):
        for s in ("short", "x" * 161):
            p = self.write("a/SKILL.md", fm(summary=s) + "body\n")
            with self.assertRaises(ps.SkillError):
                ps.build_skill(p, self.root)

    def test_shareable_long_dash_names_line(self):
        p = self.write("a/SKILL.md", fm() + "one\ntwo \u2014 three\n")
        with self.assertRaises(ps.SkillError) as c:
            ps.build_skill(p, self.root)
        self.assertIn("line 2", str(c.exception))

    def test_internal_long_dash_ok(self):
        p = self.write("a/SKILL.md", fm(pulse="internal") + "two \u2014 three\n")
        self.assertEqual(ps.build_skill(p, self.root)["audience"], "internal")

    def test_vendor_and_hardening(self):
        for bad in ["uses GHL here", "go high level", "G\u200bHL", "\uff27\uff28\uff2c", "my_GHL_API_KEY", "a\u2212b", "x\u2013y"]:
            p = self.write("a/SKILL.md", fm() + bad + "\n")
            with self.assertRaises(ps.SkillError, msg=bad):
                ps.build_skill(p, self.root)
        for ok in ["the Alright guide", "ghlx is fine", "xghl"]:
            p = self.write("a/SKILL.md", fm() + ok + "\n")
            self.assertIsNotNone(ps.build_skill(p, self.root), ok)

    def test_vendor_in_summary_refused(self):
        p = self.write("a/SKILL.md", fm(summary="Works with your GHL account") + "body\n")
        with self.assertRaises(ps.SkillError):
            ps.build_skill(p, self.root)

    def test_secret_refused_even_internal(self):
        p = self.write("a/SKILL.md", fm(pulse="internal") + "key sk-abcdefghijklmnopqrstu\n")
        with self.assertRaises(ps.SkillError):
            ps.build_skill(p, self.root)

    def test_long_name_run_is_secret_but_uuid_and_snake_are_not(self):
        p = self.write("a/SKILL.md", fm() + "id 0123456789abcdef0123456789abcdef0123\n")
        with self.assertRaises(ps.SkillError):
            ps.build_skill(p, self.root)
        p = self.write("a/SKILL.md", fm() + "id 123e4567-e89b-12d3-a456-426614174000 and follow_up_after_2_days_v3_texts\n")
        self.assertIsNotNone(ps.build_skill(p, self.root))

    def test_nul_and_leading_rule_refused(self):
        p = self.write("a/SKILL.md", fm() + "a\u0000b\n")
        with self.assertRaises(ps.SkillError):
            ps.build_skill(p, self.root)
        p = self.write("a/SKILL.md", fm() + "---\nstill frontmatter-like\n")
        with self.assertRaises(ps.SkillError):
            ps.build_skill(p, self.root)

    def test_big_body_ok_and_too_big_refused(self):
        big = ("word " * 40_000)[:146_772]
        p = self.write("a/SKILL.md", fm() + big)
        self.assertEqual(len(ps.build_skill(p, self.root)["body"]), len(big.strip()) + 1)
        p = self.write("a/SKILL.md", fm() + ("word " * 90_000))
        with self.assertRaises(ps.SkillError):
            ps.build_skill(p, self.root)

    def test_references_appended_in_name_order(self):
        p = self.write("a/SKILL.md", fm() + "main\n")
        self.write("a/references/b.md", "B text\n")
        self.write("a/references/a.md", "A text\n")
        self.write("a/references/c.txt", "ignored\n")
        body = ps.build_skill(p, self.root)["body"]
        self.assertLess(body.index("## File: references/a.md"), body.index("## File: references/b.md"))
        self.assertIn("\n\n## File: references/a.md\n\nA text", body)
        self.assertNotIn("ignored", body)

    def test_reference_with_long_dash_in_shareable_refused(self):
        p = self.write("a/SKILL.md", fm() + "main\n")
        self.write("a/references/a.md", "bad \u2014 dash\n")
        with self.assertRaises(ps.SkillError):
            ps.build_skill(p, self.root)

    def test_version_order(self):
        p = self.write("a/SKILL.md", fm() + "**Version: 0.2.0**\n")
        self.write("a/config.yaml", 'VERSION: "1.0.0"\n')
        self.assertEqual(ps.build_skill(p, self.root)["version"], "0.2.0")
        p = self.write("a/SKILL.md", fm() + "text\n")
        self.assertEqual(ps.build_skill(p, self.root)["version"], "1.0.0")
        (self.root / "a/config.yaml").unlink()
        self.write(".claude-plugin/plugin.json", json.dumps({"version": "3.1.4"}))
        self.assertEqual(ps.build_skill(p, self.root)["version"], "3.1.4")
        (self.root / ".claude-plugin/plugin.json").unlink()
        self.assertIsNone(ps.build_skill(p, self.root)["version"])

    def test_root_config_yaml(self):
        p = self.write("a/SKILL.md", fm() + "text\n")
        self.write("config.yaml", "VERSION: 2.0.0\n")
        self.assertEqual(ps.build_skill(p, self.root)["version"], "2.0.0")


class CollectTests(Base):
    def test_collect_lists_marked_only_and_skips_dirs(self):
        self.write("one/SKILL.md", fm(name="odf-one") + "x\n")
        self.write("two/SKILL.md", fm(name="odf-two", pulse=None) + "x\n")
        self.write("node_modules/z/SKILL.md", fm(name="odf-z") + "x\n")
        self.write(".github/q/SKILL.md", fm(name="odf-q") + "x\n")
        self.write("dist/d/SKILL.md", fm(name="odf-d") + "x\n")
        self.assertEqual([s["name"] for s in ps.collect(self.root)], ["odf-one"])

    def test_duplicate_names_raise(self):
        self.write("one/SKILL.md", fm() + "x\n")
        self.write("two/SKILL.md", fm() + "y\n")
        with self.assertRaises(ps.SkillError):
            ps.collect(self.root)


class WarningTests(unittest.TestCase):
    def test_shareable_warnings(self):
        w = ps.content_warnings({"audience": "shareable", "body": "it costs $5\nbig ROI here\nclean\n"})
        self.assertEqual(len(w), 2)
        self.assertTrue(any("line 1" in x for x in w))
        self.assertTrue(any("line 2" in x for x in w))

    def test_markup_and_hours(self):
        w = ps.content_warnings({"audience": "shareable", "body": "the markup\nsaves hours\n"})
        self.assertEqual(len(w), 2)

    def test_internal_none(self):
        self.assertEqual(ps.content_warnings({"audience": "internal", "body": "costs $5 ROI"}), [])


class MainTests(Base):
    def run_main(self, argv, env, urlopen=None):
        out, err = io.StringIO(), io.StringIO()
        full_env = {"GITHUB_REPOSITORY": "JD-OD/odf-demo", "GITHUB_SHA": "a" * 40}
        full_env.update(env)
        cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with mock.patch.dict(os.environ, full_env, clear=True), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    mock.patch.object(ps.urllib.request, "urlopen", urlopen or mock.Mock(side_effect=AssertionError("network"))):
                try:
                    code = ps.main(argv)
                except SystemExit as e:
                    code = e.code
        finally:
            os.chdir(cwd)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_sends_nothing_and_prints_warnings(self):
        self.write("a/SKILL.md", fm() + "costs $5\n")
        code, out, _ = self.run_main(["--dry-run"], {})
        self.assertIn(code, (0, None))
        self.assertIn("odf-demo", out)
        self.assertIn("warning", out.lower())
        self.assertIn("dry run", out)

    def test_missing_secret(self):
        self.write("a/SKILL.md", fm() + "x\n")
        code, _, err = self.run_main([], {"SKILLS_PUBLISH_SECRET": "short"})
        self.assertEqual(code, 1)
        self.assertIn("[publish] SKILLS_PUBLISH_SECRET is missing or too short; nothing sent", err)

    def test_skill_error_exits_1(self):
        self.write("a/SKILL.md", fm(summary=None) + "x\n")
        code, _, err = self.run_main(["--dry-run"], {})
        self.assertEqual(code, 1)
        self.assertIn("a/SKILL.md", err)

    def test_http_409_exits_1_with_pulse_error(self):
        self.write("a/SKILL.md", fm() + "x\n")

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(b'{"error":"odf-demo is published from JD-OD/odf-other"}'))

        code, _, err = self.run_main([], {"SKILLS_PUBLISH_SECRET": "s" * 40}, urlopen=boom)
        self.assertEqual(code, 1)
        self.assertIn("409", err)
        self.assertIn("published from JD-OD/odf-other", err)

    def test_success_payload(self):
        self.write("a/SKILL.md", fm(summary="Tr\u00e8s utile pour l'\u00e9quipe") + "caf\u00e9\n")
        seen = {}

        class R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"status":"published"}'

        def ok(req, timeout=None):
            seen["req"] = req
            seen["timeout"] = timeout
            return R()

        code, out, _ = self.run_main([], {"SKILLS_PUBLISH_SECRET": "s" * 40}, urlopen=ok)
        self.assertIn(code, (0, None))
        req = seen["req"]
        self.assertEqual(req.full_url, "https://pulse.opendealflow.com/api/webhooks/skills/publish")
        self.assertEqual(req.get_header("X-skills-secret"), "s" * 40)
        self.assertEqual(seen["timeout"], 60)
        raw = req.data.decode("utf-8")
        self.assertIn("caf\u00e9", raw)  # ensure_ascii=False
        payload = json.loads(raw)
        self.assertEqual(payload["repo"], "JD-OD/odf-demo")
        self.assertEqual(payload["commitSha"], "a" * 40)
        self.assertEqual(sorted(payload["skills"][0]), ["audience", "body", "name", "path", "summary", "version"])
        self.assertNotIn("s" * 40, out)


class SourceEscapes(unittest.TestCase):
    """The checks' invisible and dash code points are written as \\u escapes, so no tool can strip them unseen."""

    def test_script_and_tests_source_is_ascii(self):
        # The tests too: a test string holding a literal invisible character could pass with it stripped.
        for name in ("publish_skills.py", "test_publish_skills.py"):
            src = (Path(__file__).resolve().parent / name).read_bytes()
            bad = [i + 1 for i, line in enumerate(src.split(b"\n")) if any(b > 127 for b in line)]
            self.assertEqual(bad, [], f"lines with non-ASCII characters in {name}")

    def test_patterns_hold_the_code_points(self):
        for ch in "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u00ad":
            self.assertTrue(ps.INVISIBLE_RE.search("a" + ch + "b"), hex(ord(ch)))
        self.assertIn("\u200b", ps.INVISIBLE_RE.pattern)
        for ch in "\r\n\u2028\u2029\u0085\u000b\u000c":
            self.assertTrue(ps.LINE_BREAK_RE.search("a" + ch + "b"), hex(ord(ch)))
        for ch in "\u2012\u2013\u2014\u2015\u2212":
            self.assertTrue(ps.LONG_DASH_RE.search("a" + ch + "b"), hex(ord(ch)))
        self.assertIsNone(ps.LONG_DASH_RE.search("a-b"))
        self.assertIsNone(ps.INVISIBLE_RE.search("plain text"))


if __name__ == "__main__":
    unittest.main()
