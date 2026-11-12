.PHONY: help clean create update update-all plan-update-all build run email-mna-report task-cmds task-cmds-md show-ids

CLI  ?= crawlab-cli
PYTHON ?= python
THIS_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
DIR  ?= $(THIS_DIR)
CYGPATH := $(shell command -v cygpath 2>/dev/null)
DIR_CLI := $(if $(CYGPATH),$(shell cygpath -m "$(DIR)"),$(DIR))
NAME ?= techcrunch_api
CMD  ?= scrapy crawl techcrunch_api
ID   ?=
IMAGE ?= news-spider:latest
SPIDER ?= techcrunch_api
NEWS_SPIDERS ?= bloomberg techstartups techcrunch techcrunch_venture eastmoney panews rfidworld bihainews futunn tonghuashun pedaily sohu_news sina_news sina_finance traveldaily guandian laoyaoba
EMAIL_TASK_CMD ?= scrapy crawl email_report
MNA_EMAIL_TASK_CMD ?= python send_mna_email_report_v2.py --period week
TASKS_FILE ?= .crawlab_tasks.map

help:
	@echo "Usage:"
	@echo "  make create"
	@echo "  make update NAME=<mapped_name>"
	@echo "  make plan-update-all"
	@echo "  make update-all"
	@echo "  make build [IMAGE=news-spider:latest]"
	@echo "  make run [IMAGE=news-spider:latest] [SPIDER=news]"
	@echo "  make email-mna-report"
	@echo "  make task-cmds"
	@echo "  make task-cmds-md"
	@echo "  make show-ids"
	@echo ""
	@echo "Current:"
	@echo "  DIR=$(DIR)"
	@echo "  DIR_CLI=$(DIR_CLI)"
	@echo "  NAME=$(NAME)"
	@echo "  CMD=$(CMD)"
	@echo "  IMAGE=$(IMAGE)"
	@echo "  SPIDER=$(SPIDER)"
	@echo "  NEWS_SPIDERS=$(NEWS_SPIDERS)"
	@echo "  EMAIL_TASK_CMD=$(EMAIL_TASK_CMD)"
	@echo "  MNA_EMAIL_TASK_CMD=$(MNA_EMAIL_TASK_CMD)"

clean:
	@find "$(DIR)" -name "*.pyc" -type f -delete
	@find "$(DIR)" -name "*.pyo" -type f -delete
	@find "$(DIR)" -name "__pycache__" -type d -prune -exec rm -rf {} +

create: clean
	@output="$$( $(CLI) upload --create --name "$(NAME)" --cmd "$(CMD)" --dir "$(DIR_CLI)" )"; \
	echo "$$output"; \
	id="$$(echo "$$output" | grep -Eo '[a-f0-9]{24}' | head -n1)"; \
	if [ -z "$$id" ]; then \
		echo "Error: cannot parse spider ID from CLI output. Use: make update NAME=<mapped_name>"; \
		exit 1; \
	fi; \
	tmp="$$(mktemp)"; \
	if [ -f "$(DIR)/$(TASKS_FILE)" ]; then \
		grep -v -E "^$(NAME)=" "$(DIR)/$(TASKS_FILE)" > "$$tmp" || true; \
	fi; \
	echo "$(NAME)=$$id|CMD=$(CMD)" >> "$$tmp"; \
	mv "$$tmp" "$(DIR)/$(TASKS_FILE)"; \
	echo "Saved: $(NAME)=$$id -> $(DIR)/$(TASKS_FILE)"

update:
	$(PYTHON) "$(THIS_DIR)/../scripts/update_news.py" --project-dir "$(DIR)" --tasks-file "$(DIR)/$(TASKS_FILE)" --names "$(NAME)" --execute

plan-update-all:
	$(PYTHON) "$(THIS_DIR)/../scripts/update_news.py" --project-dir "$(DIR)" --tasks-file "$(DIR)/$(TASKS_FILE)" --dry-run

update-all:
	$(PYTHON) "$(THIS_DIR)/../scripts/update_news.py" --project-dir "$(DIR)" --tasks-file "$(DIR)/$(TASKS_FILE)" --execute

build:
	docker build -t "$(IMAGE)" "$(DIR)"

run:
	docker run --rm -it "$(IMAGE)" scrapy crawl "$(SPIDER)"

email-mna-report:
	$(MNA_EMAIL_TASK_CMD)

task-cmds:
	@echo "Project: $(NAME)"
	@echo "Use these commands when creating Crawlab tasks/schedules:"
	@for s in $(NEWS_SPIDERS); do \
		echo "  $$s: scrapy crawl $$s"; \
	done
	@echo "  email_report: $(EMAIL_TASK_CMD)"
	@echo "  mna_email_report: $(MNA_EMAIL_TASK_CMD)"
	@echo "    env: EMAIL_REPORT_HOURS=24 EMAIL_REPORT_LIMIT=1000 SMTP_SERVER=... SMTP_PORT=465 SMTP_USER=... SMTP_PASS=... NEWS_EMAIL_TO=..."

task-cmds-md:
	@echo "| task_name | cmd |"
	@echo "|---|---|"
	@for s in $(NEWS_SPIDERS); do \
		echo "| news_$$s | scrapy crawl $$s |"; \
	done
	@echo "| news_email_report | $(EMAIL_TASK_CMD) |"
	@echo "| news_mna_email_report | $(MNA_EMAIL_TASK_CMD) |"

show-ids:
	@if [ -f "$(DIR)/$(TASKS_FILE)" ]; then \
		cat "$(DIR)/$(TASKS_FILE)"; \
	else \
		echo "No task map found at $(DIR)/$(TASKS_FILE)"; \
	fi

techrunch_api:
	make update NAME=techcrunch_api
