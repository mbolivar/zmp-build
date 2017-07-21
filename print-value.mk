print-%:
	@if [ '$($*)' = "" ]; then echo "$* is undefined"; exit 1; fi
	@echo '$($*)'
