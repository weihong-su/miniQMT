# 依赖安装指南

## 快速安装

### 方法 1: 使用 requirements.txt (推荐)

```bash
# 1. 创建虚拟环境(推荐)
python -m venv venv

# 2. 激活虚拟环境
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# 3. 一键安装所有依赖
pip install -r utils/requirements.txt

# 4. 验证安装
python check_dependencies.py
```

### 方法 2: 手动安装

```bash
# 核心依赖
pip install pandas>=1.3.0 numpy>=1.21.0

# Web框架
pip install Flask>=2.0.0 Flask-CORS>=3.0.10

# QMT API
pip install xtquant

# 数据源
pip install mootdx baostock

# 数据验证
pip install marshmallow>=3.14.0

# HTTP请求
pip install requests>=2.26.0
```

## 依赖说明

### 核心依赖(必需)

| 包名 | 版本要求 | 用途 |
|------|---------|------|
| **pandas** | >=1.3.0 | 数据处理与分析 |
| **numpy** | >=1.21.0 | 数值计算 |
| **Flask** | >=2.0.0 | Web服务框架 |
| **Flask-CORS** | >=3.0.10 | 跨域资源共享 |
| **xtquant** | >=1.0.0 | 迅投QMT交易接口 |
| **mootdx** | >=0.4.0 | 通达信行情数据 |
| **baostock** | >=0.8.8 | 宝塔金融数据 |
| **marshmallow** | >=3.14.0 | 数据验证 |
| **requests** | >=2.26.0 | HTTP请求库 |

### 可选依赖(性能优化)

```bash
# pandas性能加速
pip install numexpr>=2.8.0 bottleneck>=1.3.0

# Excel文件支持
pip install openpyxl>=3.0.0 xlrd>=2.0.0

# K线图绘制
pip install mplfinance>=0.12.0

# MySQL数据库支持
pip install pymysql>=1.0.0

# 定时任务调度
pip install schedule>=1.1.0
```

### 技术分析库 TA-Lib (特殊安装)

TA-Lib 需要从本地 whl 文件安装（不在 PyPI 仓库）：

```bash
# Python 3.8 用户
pip install utils/TA_Lib-0.4.26-cp38-cp38-win_amd64.whl

# 其他Python版本需下载对应的whl文件
# cp38 = Python 3.8
# cp39 = Python 3.9
# cp310 = Python 3.10
```

**下载地址**: [https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib](https://www.lfd.uci.edu/~gohlke/pythonlibs/#ta-lib)

**注意**: 项目已包含 Python 3.8 的 whl 文件，但当前代码中未使用 TA-Lib
pip install openpyxl>=3.0.0 xlrd>=2.0.0
```

### 开发依赖(开发调试)

```bash
# 测试框架
pip install pytest>=6.2.0 pytest-cov>=2.12.0

# 代码质量
pip install black>=21.0 flake8>=3.9.0
```

## 常见问题

### Q1: pip install 速度慢怎么办?

**A**: 使用国内镜像源加速

```bash
# 临时使用清华镜像
pip install -r utils/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 或永久配置
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

**常用国内镜像**:
- 清华: `https://pypi.tuna.tsinghua.edu.cn/simple`
- 阿里: `https://mirrors.aliyun.com/pypi/simple/`
- 腾讯: `https://mirrors.cloud.tencent.com/pypi/simple`

### Q2: xtquant 安装失败?

**A**: xtquant 需要从QMT客户端安装目录获取

1. 确保已安装QMT客户端
2. 找到QMT安装目录(如: `C:\光大证券金阳光QMT实盘\userdata_mini`)
3. 手动安装xtquant:
   ```bash
   # 方法1: 使用QMT提供的安装包
   cd C:\光大证券金阳光QMT实盘\userdata_mini\datadir\whl
   pip install xtquant-*.whl

   # 方法2: 如果QMT已安装,添加到Python路径
   # 在代码中添加: sys.path.append('QMT安装路径')
   ```

### Q3: 安装时出现权限错误?

**A**: 使用 `--user` 参数或管理员权限

```bash
# 用户级安装(推荐)
pip install -r utils/requirements.txt --user

# 或使用管理员权限运行命令提示符
```

### Q4: pandas/numpy 版本冲突?

**A**: 升级 pip 并清理缓存

```bash
# 升级pip
python -m pip install --upgrade pip

# 清理缓存
pip cache purge

# 重新安装
pip install -r utils/requirements.txt --force-reinstall
```

### Q5: 如何验证安装是否成功?

**A**: 运行依赖检查脚本

```bash
python check_dependencies.py
```

**预期输出**:
```
============================================================
miniQMT 依赖包检查
============================================================

检查核心依赖包:
------------------------------------------------------------
✓ OK  pandas               1.5.3
✓ OK  numpy                1.24.3
✓ OK  Flask                2.3.2
✓ OK  Flask-CORS           4.0.0
✓ OK  xtquant              1.1.0
✓ OK  mootdx               0.4.5
✓ OK  baostock             0.8.9
✓ OK  marshmallow          3.20.1
------------------------------------------------------------

检查完成: 8/8 个包已正确安装

✓ 所有依赖包检查通过!
```

## 下一步

安装完成后:

1. **配置账户**: 创建 `account_config.json` 文件
   ```json
   {
     "account_id": "您的交易账号",
     "account_type": "STOCK",
     "qmt_path": "C:/光大证券金阳光QMT实盘/userdata_mini"
   }
   ```

2. **配置股票池**: 创建 `stock_pool.json` 文件(可选)
   ```json
   [
     "000001.SZ",
     "600036.SH"
   ]
   ```

3. **启动系统**:
   ```bash
   python main.py
   ```

4. **访问前端**: 浏览器打开 `http://localhost:5000`

## 技术支持

如遇到安装问题,请查看:
- [README.md](README.md) - 项目说明
- [CLAUDE.md](CLAUDE.md) - 开发指南
- [GitHub Issues](https://github.com/your-repo/miniQMT/issues) - 提交问题
