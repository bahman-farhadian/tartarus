.DEFAULT_GOAL := help

.PHONY: help web practice report init

help: ## Show available Tartarus commands.
	@printf '%s\n' \
		'Tartarus commands:' \
		'  make web                              Start the local web UI' \
		'  make practice user=<name> list=<name> Start a CLI practice session' \
		'  make report user=<name> [list=<name>] Show a progress report' \
		'  make init user=<name> list=<name>     Create an empty word list' \
		'' \
		'Optional CLI flags can be passed with opts, for example:' \
		'  make practice user=bahman list=german opts="--no-audio"'

web: ## Start the localhost web UI.
	@./tartarus_web.sh

practice: ## Start a CLI practice session (requires user and list).
	@test -n "$(user)" || { echo 'Missing user=<name>'; exit 2; }
	@test -n "$(list)" || { echo 'Missing list=<name>'; exit 2; }
	@./tartarus.sh practice --user "$(user)" --lang "$(list)" $(opts)

report: ## Show a report (requires user; list is optional).
	@test -n "$(user)" || { echo 'Missing user=<name>'; exit 2; }
	@./tartarus.sh report --user "$(user)" $(if $(list),--lang "$(list)")

init: ## Create an empty word list (requires user and list).
	@test -n "$(user)" || { echo 'Missing user=<name>'; exit 2; }
	@test -n "$(list)" || { echo 'Missing list=<name>'; exit 2; }
	@./tartarus.sh init --user "$(user)" --lang "$(list)"
