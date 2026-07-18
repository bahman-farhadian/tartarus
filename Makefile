.DEFAULT_GOAL := help

.PHONY: help web practice report init video

help: ## Show available Tartarus commands.
	@printf '%s\n' \
		'Tartarus commands:' \
		'  make web                              Start the local web UI' \
		'  make practice user=<name> list=<name> Start a CLI practice session' \
		'  make report user=<name> [list=<name>] Show a progress report' \
		'  make init user=<name> list=<name>     Create an empty word list' \
		'  make video opts="<options>"            Generate a vocabulary video' \
		'' \
		'Optional CLI flags can be passed with opts, for example:' \
		'  make practice user=bahman list=german opts="--no-audio"'

web: ## Start the localhost web UI.
	@python3 utils/tartarus_web.py $(opts)

practice: ## Start a CLI practice session (requires user and list).
	@test -n "$(user)" || { echo 'Missing user=<name>'; exit 2; }
	@test -n "$(list)" || { echo 'Missing list=<name>'; exit 2; }
	@python3 utils/tartarus.py practice --user "$(user)" --lang "$(list)" $(opts)

report: ## Show a report (requires user; list is optional).
	@test -n "$(user)" || { echo 'Missing user=<name>'; exit 2; }
	@python3 utils/tartarus.py report --user "$(user)" $(if $(list),--lang "$(list)")

init: ## Create an empty word list (requires user and list).
	@test -n "$(user)" || { echo 'Missing user=<name>'; exit 2; }
	@test -n "$(list)" || { echo 'Missing list=<name>'; exit 2; }
	@python3 utils/tartarus.py init --user "$(user)" --lang "$(list)"

video: ## Generate a vocabulary-drill video (pass options with opts).
	@python3 utils/make_tartarus_video.py $(opts)
