# BlackAgent 答辩与验收材料

本目录包含本次生成的最终交付物：

- `BlackAgent_答辩PPT.pptx`：12 页答辩 PPT，围绕阶段目标、核心挑战、实现证据、评测边界和下一步优化组织。
- `BlackAgent_验收报告.docx`：可提交的 Word 版验收报告。
- `BlackAgent_验收报告.md`：同内容 Markdown 版，便于仓库内审阅和后续维护。

验证摘要：

- PPTX 使用 artifact-tool 生成并渲染预览，布局检查 0 error（仍有少量 tight-text warning，不影响预览可读性）。
- DOCX 使用 Word COM 导出 PDF，并通过 `pdftoppm` 转成 6 页 PNG 逐页检查，未发现文字重叠或表格截断。
- 代码侧执行 `python -m compileall -q src scripts tests main.py` 通过。

边界说明：报告中保留 manual held-out、OCR hardset、source skew 和线上生产化边界，避免把 smoke test 或局部验证包装成生产效果。
