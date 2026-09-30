"""Offline check: run with python .github/scripts/test_stats.py."""
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import gen_stats
from gen_stats import TEXT, placeholder, render


for sample in (False, True):
    data = placeholder('YouKyi <&> "', sample=sample)
    data['summary'] = 'sample' if sample else 'public activity summary'
    for language, labels in TEXT.items():
        for mobile in (False, True):
            svg = render(data, mobile=mobile, language=language)
            root = ET.fromstring(svg)
            text = ''.join(root.itertext())
            assert root.attrib['viewBox'].split()[2] == ('420' if mobile else '820')
            assert root.attrib['{http://www.w3.org/XML/1998/namespace}lang'] == language
            assert data['login'] in text  # XML escaping preserves the actual label.
            assert (labels['sample'] in text) == sample
            assert ('1 342' in text) == sample
            assert (labels['unavailable'] in text) != sample
            assert labels['activity'] in text
            assert all(label in text for label in labels['metrics'])
            assert '@font-face' in svg and 'data:font/woff2;base64,' in svg
            assert 'prefers-color-scheme:dark' in svg
            assert not any(old in svg for old in ('Gradient', 'feGaussianBlur', '#BF00FF'))

# The CI command must fetch once, then write all four cards from that result.
with TemporaryDirectory() as directory:
    output = Path(directory) / 'stats.svg'
    with patch.dict('os.environ', {'GH_TOKEN': 'offline-test'}), \
         patch('sys.argv', ['gen_stats.py', '--out', str(output)]), \
         patch.object(gen_stats, 'fetch', return_value=placeholder('YouKyi', sample=True)) as fetch:
        gen_stats.main()
        fetch.assert_called_once()
    assert {p.name for p in Path(directory).iterdir()} == {
        'stats.svg', 'stats-mobile.svg', 'stats-en.svg', 'stats-en-mobile.svg',
    }
    for p in Path(directory).iterdir():
        text = ''.join(ET.parse(p).getroot().itertext())
        assert '1 342' in text and '220 608' in text

print('Stats: both languages/layouts, escaping, samples and single-fetch generation OK.')
