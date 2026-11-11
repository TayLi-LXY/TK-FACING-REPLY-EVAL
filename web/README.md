# Web UI

FastAPI + Vite/React 闭环工作台。

## 启动

在项目根目录：

```bash
pip install -r requirements-web.txt
python -m uvicorn web.backend.main:app --reload --host 127.0.0.1 --port 8765
```

另开终端：

```bash
cd web/frontend
npm install
npm run dev
```

打开 http://localhost:5173
