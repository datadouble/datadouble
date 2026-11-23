# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

from pathlib import Path

import format_yaml
import pytest

from datadouble.programs import Completed

FLOW = """\
replicate:
  using:
    k8s-pv: {path: /srv/clones, local: {node: worker-1}, kubeconfig: {env: DEV_KUBECONFIG}}
store: [{filesystem: {path: /x}}, {s3: {bucket: "a, b}"}}, plain]
include: [billing.*]
empty: {}
none: []
items:
  - {table: x, columns: [a, b]}
  - - {a: 1}
  - [[{b: 2}], c]
next: {q: 'it''s, ok', r: "x\\"}y", s: , t: http://h:1/p, u: {}, v: [ ], w: [{}]}
multi: {a: 1,
  b: 2}
"""

BLOCK = """\
replicate:
  using:
    k8s-pv:
      path: /srv/clones
      local:
        node: worker-1
      kubeconfig:
        env: DEV_KUBECONFIG
store:
  - filesystem:
      path: /x
  - s3:
      bucket: "a, b}"
  - plain
include: [billing.*]
empty: {}
none: []
items:
  - table: x
    columns: [a, b]
  - - a: 1
  - - - b: 2
    - c
next:
  q: 'it''s, ok'
  r: "x\\"}y"
  s:
  t: http://h:1/p
  u: {}
  v: []
  w:
    - {}
multi:
  a: 1
  b: 2
"""


def test_flow_mappings_become_block_mappings() -> None:
    result = format_yaml.format_yaml(FLOW)
    assert result.text == BLOCK
    assert result.changed == [3, 4, 9, 10, 11, 12, 13]
    assert result.problems == []
    again = format_yaml.format_yaml(result.text)
    assert (again.text, again.changed) == (BLOCK, [])


def test_comments_keep_their_column() -> None:
    text = "a:\n  b: {c: 1}      # why\n  - {d: 2}  # item\n  long_key_here: {e: 1} # tight\n"
    assert format_yaml.format_text(text).text == (
        "a:\n  b:             # why\n    c: 1\n  - d: 2    # item\n"
        "  long_key_here:        # tight\n    e: 1\n"
    )


def test_what_stays() -> None:
    text = (
        "# a: {b: 1}\n"
        "run: |\n  x: {a: 1}\n\n  y: {b: 2}\nafter: {c: 3}\n"
        "plain: text\n"
        'roleRef: {kind: Role, name: {{ include "x" . }}}\n'
        "value: {{ .Values.x }}\n"
        "- just a scalar\n"
    )
    result = format_yaml.format_text(text)
    assert result.text == (
        "# a: {b: 1}\n"
        "run: |\n  x: {a: 1}\n\n  y: {b: 2}\nafter:\n  c: 3\n"
        "plain: text\n"
        'roleRef:\n  kind: Role\n  name: {{ include "x" . }}\n'
        "value: {{ .Values.x }}\n"
        "- just a scalar\n"
    )
    assert result.changed == [6, 8]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("a: {b: &x 1}", "an anchor, tag or complex key"),
        ("a: {[b]: 1}", "an anchor, tag or complex key"),
        ("a: {b}", "a key without a value"),
        ("a: {b: 1 # c}", "a comment inside a flow collection"),
        ('a: {b: "c" d}', "an unexpected character in a flow collection"),
        ("a: [b: 1, {c: 2}]", "a single-pair mapping in a flow sequence"),
        ("a: {b: {{ c }", "an unterminated template action"),
        ("a: {b: 1} c", "text after a flow collection"),
        ("a: {b: 'c", "an unterminated flow collection"),
        ("a: {b: 1,  # c\n  d: 2}", "an unterminated flow collection"),
    ],
)
def test_problems(text: str, problem: str) -> None:
    result = format_yaml.format_text(text)
    assert (result.text, result.changed, result.problems) == (text, [], [(1, problem)])


def test_a_long_unterminated_collection() -> None:
    text = "a: {b: 1,\n" + "  c: 2,\n" * 60
    assert format_yaml.format_text(text).problems == [(1, "an unterminated flow collection")]


def test_a_rewrite_that_changes_the_data_is_refused() -> None:
    text = "k: x\n  - {a: 1}\n"
    assert format_yaml.format_yaml(text).changed == [2]
    loads = "k: >\n  x\nnot: {a: 1}\n"
    assert format_yaml.format_yaml(loads).changed == [3]
    changing = 'k: "x\n  y: {a: 1}\n  "\n'
    original = format_yaml.format_yaml(changing)
    assert original.text == changing
    assert original.problems == [(2, "the rewrite changes the data")]


def test_markdown() -> None:
    text = (
        "Intro `a: {b: 1}`.\n\n"
        "```yaml\na: {b: 1}\n```\n\n"
        "1. Item:\n\n    ```yml\n    c: {d: 2}\n\n    e: [f]\n    ```\n\n"
        "```yaml\nalready: block\n```\n"
        "```python\nx = {'a': {'b': 1}}\n```\n"
        "```yaml\nbad: {g}\n```\n"
        "~~~yaml\nopen: {h: 1}\n"
    )
    result = format_yaml.format_markdown(text)
    assert result.text == (
        "Intro `a: {b: 1}`.\n\n"
        "```yaml\na:\n  b: 1\n```\n\n"
        "1. Item:\n\n    ```yml\n    c:\n      d: 2\n\n    e: [f]\n    ```\n\n"
        "```yaml\nalready: block\n```\n"
        "```python\nx = {'a': {'b': 1}}\n```\n"
        "```yaml\nbad: {g}\n```\n"
        "~~~yaml\nopen:\n  h: 1\n"
    )
    assert result.changed == [4, 10, 25]
    assert result.problems == [(22, "a key without a value")]


PYTHON = '''\
QUOTED = """
k: "x
  y: {a: 1}
  "
"""

SPEC = """
name: shop
source: {engine: fake}
"""
TEMPLATE = """
store: [{{filesystem: {{path: {base}/store}}}}]
{extra}
"""
FIELDS = f"""
promote: {{to: {{path: {tmp / "x"}, n: {f"{n}"}}}}}
"""
FORMATTED = """
a: {{b: {c}}}
""".format(c=1)
PERCENT = """
a: {b: %s}
""" % 1
JSON = """
{"a": {"b": 1}}
"""
ESCAPED = """
a: {b: "\\\\d"}
"""
BYTES = b"""
a: {b: 1}
"""
ONE_LINE = "a: {b: 1}\\n"
BROKEN = """
a: {{b: {c
"""
LONE = """
a: {{b: 1}}}
"""
CONSTANT = """
a: {{b: {c}}}
"""
PROSE = """
not: yaml: {a: 1}
"""
'''

PYTHON_FORMATTED = '''\
QUOTED = """
k: "x
  y: {a: 1}
  "
"""

SPEC = """
name: shop
source:
  engine: fake
"""
TEMPLATE = """
store:
  - filesystem:
      path: {base}/store
{extra}
"""
FIELDS = f"""
promote:
  to:
    path: {tmp / "x"}
    n: {f"{n}"}
"""
FORMATTED = """
a:
  b: {c}
""".format(c=1)
PERCENT = """
a: {b: %s}
""" % 1
JSON = """
{"a": {"b": 1}}
"""
ESCAPED = """
a: {b: "\\\\d"}
"""
BYTES = b"""
a: {b: 1}
"""
ONE_LINE = "a: {b: 1}\\n"
BROKEN = """
a: {{b: {c
"""
LONE = """
a: {{b: 1}}}
"""
CONSTANT = """
a:
  b: {c}
"""
PROSE = """
not: yaml: {a: 1}
"""
'''


def test_python() -> None:
    result = format_yaml.format_python(PYTHON)
    assert result.text == PYTHON_FORMATTED
    assert result.changed == [9, 12, 16, 19, 41]
    assert result.problems == [(3, "the rewrite changes the data")]


def test_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    flow = tmp_path / "flow.yml"
    flow.write_text("a: {b: 1}\n", "utf-8")
    block = tmp_path / "block.yaml"
    block.write_text("a:\n  b: 1\n", "utf-8")
    bad = tmp_path / "bad.md"
    bad.write_text("```yaml\na: {b}\n```\n", "utf-8")
    assert format_yaml.main(["--check", str(flow), str(block)]) == 1
    assert f"{flow}:1: flow mapping, write it in block style" in capsys.readouterr().out
    assert flow.read_text("utf-8") == "a: {b: 1}\n"
    assert format_yaml.main([str(flow), str(block)]) == 0
    assert "1 file(s) formatted, 0 problem(s)" in capsys.readouterr().out
    assert flow.read_text("utf-8") == "a:\n  b: 1\n"
    assert format_yaml.main(["--check", str(flow)]) == 0
    assert format_yaml.main([str(bad)]) == 1
    assert f"{bad}:2: cannot rewrite: a key without a value" in capsys.readouterr().out

    monkeypatch.chdir(tmp_path)

    def listing(_program: str, _arguments: list[str]) -> Completed:
        return Completed(0, "flow.yml\nbad.md\nscript.sh\ngone.yml\n", "")

    monkeypatch.setattr(format_yaml, "run_program", listing)
    assert format_yaml.tracked_files() == [Path("flow.yml"), Path("bad.md")]
    assert format_yaml.main(["--check"]) == 1


def test_tracked_files_needs_git(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(_program: str, _arguments: list[str]) -> Completed:
        return Completed(128, "", "not a repo")

    monkeypatch.setattr(format_yaml, "run_program", failing)
    with pytest.raises(RuntimeError, match="git ls-files failed: not a repo"):
        format_yaml.tracked_files()
