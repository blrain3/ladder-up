.PHONY: help diagrams docs clean

help:
	@echo "make diagrams  # 由 puml 出 PNG"
	@echo "make docs      # 文档转 docx/pdf"
	@echo "make clean     # 清理中间产物"

diagrams:
	@bash scripts/render-diagrams.sh

docs:
	@bash scripts/build-docs.sh

clean:
	@rm -rf docs/_build
	@find . -name '*.tmp' -delete
	@echo "done"
