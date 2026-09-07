"""Small offline drift gates for the public documentation's source contract."""
from __future__ import annotations

import ast
import json
import re
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / '.super-coder'
CURRENT_DOCS = [
    ROOT / 'README.md', ROOT / 'docs/quick-start.md', ROOT / 'docs/README.md',
    ENGINE / 'README.md', *sorted((ENGINE / 'adapters').glob('*/README.md')),
    *sorted((ENGINE / 'docs').glob('*.md')),
]
LINK_DOCS = [*CURRENT_DOCS, ROOT / 'docs/deepseek-harness-removal.md']
RAW_PREFIX = 'https://raw.githubusercontent.com/jedbjorn/subfloor/main/'


def headings(text):
    """GitHub-style heading IDs, including duplicate suffixes; ignore code."""
    counts = Counter()
    result = set()
    fenced = False
    for line in text.splitlines():
        if line.startswith('```'):
            fenced = not fenced
        if fenced or not re.match(r'^#{1,6} ', line):
            continue
        label = re.sub(r'^#+ | +#+$', '', line).strip().lower()
        label = re.sub(r'[^\w\- ]', '', label).replace(' ', '-')
        index = counts[label]
        counts[label] += 1
        result.add(label + (f'-{index}' if index else ''))
    return result


class PublicDocumentationTest(unittest.TestCase):
    def test_internal_files_anchors_and_raw_images_exist(self):
        for path in LINK_DOCS:
            for match in re.finditer(r'(!?)\[[^\]\n]*\]\(([^\s)]+)\)', path.read_text()):
                image, target = match.groups()
                with self.subTest(document=str(path.relative_to(ROOT)), target=target):
                    if target.startswith(RAW_PREFIX):
                        local = ROOT / unquote(target[len(RAW_PREFIX):])
                        self.assertTrue(local.is_file(), str(local))
                        continue
                    parsed = urlsplit(target)
                    if parsed.scheme or parsed.netloc:
                        continue
                    self.assertFalse(image, 'themed images need absolute URLs')
                    local = path.parent / unquote(parsed.path) if parsed.path else path
                    self.assertTrue(local.exists(), str(local))
                    if parsed.fragment and local.suffix == '.md':
                        self.assertIn(unquote(parsed.fragment), headings(local.read_text()))

    def test_documented_command_families_exist_in_dispatcher(self):
        # Read the actual command case, without executing any maintenance verb.
        dispatch = (ENGINE / 'scripts/dispatch.sh').read_text().split('case "$cmd" in')[-1]
        families = set()
        for label in re.findall(r'^  ([a-zA-Z0-9_|*\-]+)\)', dispatch, re.MULTILINE):
            families.update(label.split('|'))
        seen = set()
        for path in CURRENT_DOCS:
            for command in re.findall(r'(?<![\w/-])(?:\./sc|sc|subfloor) ([a-z][a-z-]+)\b', path.read_text()):
                with self.subTest(document=str(path.relative_to(ROOT)), command=command):
                    self.assertTrue(command in families or any(
                        item.endswith('*') and command.startswith(item[:-1])
                        for item in families
                    ), 'documented command is absent from dispatcher')
                    seen.add(command)
        self.assertTrue({'install', 'enter', 'sprint', 'remove', 'sandbox-memory'} <= seen)

    def test_harness_flavor_roster_and_tab_facts_match_source(self):
        guide = (ROOT / 'docs/README.md').read_text()
        adapters = [json.loads(p.read_text()) for p in (ENGINE / 'adapters').glob('*/adapter.json')]
        self.assertIn(f'value: {len(adapters)}\nlabel: Coding harnesses', guide)
        flavors = {p.stem for p in (ENGINE / 'templates/shells').glob('*.json')}
        self.assertIn(f'value: {len(flavors)}\nlabel: Shell flavors', guide)
        for flavor in flavors:
            self.assertIn(flavor, guide)
        tree = ast.parse((ENGINE / 'scripts/init_fork.py').read_text())
        roster = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign) and any(
                          isinstance(target, ast.Name) and target.id == 'TEAM_ROSTER'
                          for target in node.targets))
        self.assertEqual(Counter(row[0] for row in roster),
                         {'admin': 1, 'planner': 2, 'dev': 4, 'reviewer': 2})
        self.assertEqual(len(roster) + 1, 10)  # singleton Cartographer is separate
        self.assertIn('ten-shell team', guide)
        tabs = re.findall(r'<button data-tab="[^"]+"[^>]*>([^<]+)</button>',
                          (ENGINE / 'ui/index.html').read_text())
        self.assertIn(f'value: {len(tabs)}\nlabel: Review-GUI tabs', guide)
        navigation = guide.split('## Review GUI\n', 1)[1].split('### ', 1)[0]
        documented_tabs = re.findall(r'^\| \*\*([^*]+)\*\* \|', navigation, re.MULTILINE)
        self.assertEqual(set(documented_tabs), set(tabs))
        vibe = next(adapter for adapter in adapters if adapter['harness'] == 'vibe')
        self.assertEqual(vibe['surfaces'],
                         {'terminal': True, 'one_shot': False, 'browser': False, 'sprint': False})
        self.assertIn('Vibe supports terminal work but does not advertise browser or Sprint', guide)

    def test_current_docs_do_not_restore_retired_contracts(self):
        # Historical migration documents are deliberately outside this scan.
        forbidden = [r'four harness(?:es)?', r'five flavors', r'nine (?:GUI[- ]?)?tabs',
                     r'six-shell (?:team|roster)', r'enter-dev\b', r'shell/dev\b',
                     r'\./sc enter dev\b', r'\.sc-worktrees/dev\b',
                     r'\.super-coder/memory\.db', r'/api/interface/(?:bootstrap|lease)',
                     r'conductor shell']
        for path in CURRENT_DOCS:
            for pattern in forbidden:
                with self.subTest(document=str(path.relative_to(ROOT)), pattern=pattern):
                    self.assertIsNone(re.search(pattern, path.read_text(), re.IGNORECASE))

    def test_themed_guide_has_supported_structure_and_current_assets(self):
        guide = (ROOT / 'docs/README.md').read_text()
        self.assertTrue(guide.startswith('---\n'))
        self.assertRegex(guide, r'(?m)^tags: \[.*\]$')
        self.assertLessEqual(len(re.findall(r'^## ', guide, re.MULTILINE)), 25)
        self.assertNotRegex(guide, r'(?m)^#{4,6} ')
        self.assertIn('## Sprints\n', guide)
        for asset in ['cli-picker', 'roadmap-tab', 'roadmap-flow', 'worktrees-tab', 'chats-tab', 'sprints-tab']:
            self.assertIn(RAW_PREFIX + f'docs/images/{asset}.png', guide)
        # Animation header proves this is still an actual GIF, not a placeholder.
        self.assertIn((ROOT / 'docs/demo.gif').read_bytes()[:6], (b'GIF87a', b'GIF89a'))


if __name__ == '__main__':
    unittest.main()
