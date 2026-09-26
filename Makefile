.PHONY: up verify down test smoke

up:            ## 构建并启动编队控制台（HOST_PORT=9090 make up 可改宿主机端口）
	docker compose up --build -d app

verify:        ## 验收：构建产物 + 代码测试 + HTTP 冒烟，以退出码报告
	./scripts/verify.sh

down:          ## 停止并清理
	docker compose down -v

test:          ## 本地运行代码测试（无需 Docker）
	python3 -m unittest discover -s tests -v

smoke:         ## 本地启动服务并执行 HTTP 冒烟（无需 Docker）
	@python3 -m app.server & \
	pid=$$!; trap "kill $$pid 2>/dev/null || true" EXIT; \
	sleep 1; APP_URL=http://localhost:8080 python3 tests/smoke.py
