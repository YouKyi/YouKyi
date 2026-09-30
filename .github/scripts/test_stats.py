"""Offline check: run with python .github/scripts/test_stats.py."""
import xml.etree.ElementTree as ET

from gen_stats import placeholder, render


for sample in (False, True):
    data = placeholder('YouKyi <&> "', sample=sample)
    data['summary'] = 'sample' if sample else 'public activity summary'
    for mobile in (False, True):
        svg = render(data, mobile=mobile)
        root = ET.fromstring(svg)
        text = ''.join(root.itertext())
        assert root.attrib['viewBox'].split()[2] == ('420' if mobile else '820')
        assert data['login'] in text  # XML escaping preserves the actual label.
        assert ('Exemple · données fictives' in text) == sample
        assert ('1 342' in text) == sample
        assert ('Non disponibles' in text) != sample
        assert '@font-face' in svg and 'data:font/woff2;base64,' in svg
        assert 'prefers-color-scheme:dark' in svg
        assert not any(old in svg for old in ('Gradient', 'feGaussianBlur', '#BF00FF'))

print('Stats: placeholder, sample, XML escaping and both layouts OK.')
