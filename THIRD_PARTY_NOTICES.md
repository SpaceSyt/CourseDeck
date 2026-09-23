# Third-party notices

Dependency packages are installed separately and carry their respective licenses. No whole
third-party application has been forked or bundled.

[pypdf](https://github.com/py-pdf/pypdf) is used for local PDF text extraction and is
distributed under BSD-3-Clause (verified against the installed 6.18.0 package metadata).
Its license is included with the separately installed package.

[react-markdown](https://github.com/remarkjs/react-markdown) and
[remark-gfm](https://github.com/remarkjs/remark-gfm) render chat Markdown and GFM tables.
Both use the MIT license, included in their installed packages.

Windows tray integration uses [pystray](https://github.com/moses-palmer/pystray)
(LGPLv3) and [Pillow](https://github.com/python-pillow/Pillow) (MIT-CMU).
These packages are installed separately with their license files; license names
were checked against the installed package metadata.

The Gradescope parser was developed with reference to student-page endpoint and markup
observations in [nyuoss/gradescope-api](https://github.com/nyuoss/gradescope-api) at commit
`586ad513b6b4715682f09fc6fe3fff2e73304f74`. Its actual `LICENSE.md` at that commit was read.
The password login and write operations were not reused. The following notice is retained
for the referenced parsing work:

MIT License

Copyright (c) 2024 gradescope-api

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
