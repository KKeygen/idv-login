manual_login_channels = [
    {
        "name": "小米账号",
        "channel": "xiaomi_app",
    },
    {"name": "华为账号", "channel": "huawei"},
    {"name": "vivo账号", "channel": "nearme_vivo"},
    {"name": "应用宝（微信）", "channel": "myapp"},
    {"name": "应用宝（QQ）", "channel": "myapp_qq"},
    {"name": "Oppo账号", "channel": "oppo"},
    {"name": "哔哩哔哩账号", "channel": "bilibili_sdk"},
    {"name": "荣耀账号", "channel": "honor_sdk"},
    {"name": "九游账号", "channel": "uc_platform"},
    {"name": "4399账号", "channel": "4399com"},
]


html = r"""<!DOCTYPE html>
<html lang="zh-cn">

<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>渠道服账号管理</title>
    
    <script src="https://lf9-cdn-tos.bytecdntp.com/cdn/expire-1-y/sweetalert/2.1.2/sweetalert.min.js"></script>
    <link href="https://lf26-cdn-tos.bytecdntp.com/cdn/expire-1-y/bootstrap/5.1.3/css/bootstrap.css" rel="stylesheet">
    <script src="https://lf3-cdn-tos.bytecdntp.com/cdn/expire-1-y/bootstrap/5.1.3/js/bootstrap.bundle.min.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.8.1/font/bootstrap-icons.css">
    <style>
        body {
            background-color: #f8f9fa;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        }
        
        .page-header {
            background-color: #3f6ad8;
            color: white;
            padding: 1.5rem 0;
            margin-bottom: 2rem;
            border-radius: 0 0 10px 10px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .card {
            border: none;
            border-radius: 10px;
            box-shadow: 0 0.25rem 0.75rem rgba(0, 0, 0, 0.1);
            margin-bottom: 1.5rem;
            transition: transform 0.2s;
        }
        
        .card:hover {
            transform: translateY(-3px);
        }
        
        .card-header {
            font-weight: 500;
            background-color: rgba(0,0,0,0.02);
            border-bottom: 1px solid rgba(0,0,0,0.03);
        }
        
        .table {
            box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.05);
            border-radius: 0.5rem;
            overflow: hidden;
        }
        
        .btn-action {
            padding: 0.25rem 0.5rem;
            font-size: 0.875rem;
            border-radius: 4px;
            margin-right: 3px;
        }
        
        .section-title {
            font-size: 1.1rem;
            font-weight: 500;
            color: #495057;
            margin-bottom: 1rem;
            border-left: 3px solid #3f6ad8;
            padding-left: 0.75rem;
        }
        
        .form-control, .form-select {
            border-radius: 0.375rem;
        }
        
        .btn-group {
            border-radius: 0.375rem;
            overflow: hidden;
        }
        
        .action-label {
            font-size: 0.75rem;
            display: block;
            text-align: center;
            margin-top: 2px;
            color: #6c757d;
        }
        
        .actions-container {
            display: flex;
            gap: 5px;
            align-items: center;
        }
        
        .action-button {
            display: flex;
            flex-direction: column;
            align-items: center;
        }
        
        .table th {
            font-size: 0.875rem;
            font-weight: 500;
        }
    </style>
</head>

<body>
    <!-- 页面头部 -->
    <div class="page-header text-center">
        <div class="container">
            <h1 class="fw-bold fs-3">渠道服账号管理系统</h1>
            <p class="lead fs-6 mb-0">轻松管理您的游戏账号和登录信息</p>
        </div>
    </div>

    <div class="container">
        <!-- 游戏选择区域 -->
        <div class="card mb-4">
            <div class="card-header d-flex justify-content-between align-items-center">
                <span><i class="bi bi-controller me-2"></i>游戏选择</span>
            </div>
            <div class="card-body">
                <div class="row align-items-center">
                    <div class="col-md-6">
                        <label for="gameSelect" class="form-label fw-bold">当前游戏:</label>
                        <select id="gameSelect" class="form-select" onchange="switchGame(this.value)">
                            <option value="" disabled selected>加载中...</option>
                        </select>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 操作区域 -->
        <div class="row">
            <!-- 登录操作 -->
            <div class="col-md-6">
                <div class="card h-100">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="bi bi-box-arrow-in-right me-2"></i>账号登录</span>
                    </div>
                    <div class="card-body">
                        <div class="mb-3">
                            <label for="channelSelect" class="form-label">渠道选择:</label>
                            <div class="d-flex align-items-center">
                                <select id="channelSelect" class="form-select me-2" style="width: 80%; flex: 0 0 auto;"></select>
                                <button onclick="manual()" class="btn btn-primary">
                                    <i class="bi bi-person-plus me-1"></i>手动登录
                                </button>
                            </div>
                        </div>
                        <div class="mb-3">
                            <p class="mb-1">当前自动登录账号：</p>
                            <div class="d-flex align-items-center">
                                <strong id="default" class="badge bg-info text-white p-2">Empty</strong>
                                <button onclick="clearDefault()" class="btn btn-outline-secondary ms-2 btn-sm">
                                    <i class="bi bi-x-circle me-1"></i>清除自动登录
                                </button>
                            </div>
                        </div>
                        <!-- 添加登录延迟设置 -->
                        <div class="mt-3">
                            <h5 class="section-title">自动登录延迟设置</h5>
                            <div class="d-flex align-items-center">
                                <input type="number" id="loginDelayInput" style="width: 80%; flex: 0 0 auto;" class="form-control me-2" min="0" max="30" placeholder="延迟秒数">
                                <button onclick="saveLoginDelay()" class="btn btn-outline-primary">
                                    <i class="bi bi-save me-1"></i>保存
                                </button>
                            </div>
                            <small class="form-text text-muted mt-1">二维码显示后<span id="currentDelay">加载中...</span> 秒会自动登录默认账号。</small>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- 系统设置 -->
            <div class="col-md-6">
                <div class="card h-100">
                    <div class="card-header d-flex justify-content-between align-items-center">
                        <span><i class="bi bi-gear me-2"></i>自动启动/关闭设置</span>
                    </div>
                    <div class="card-body">
                        <button id="autoCloseBtn" onclick="switchAutoClose()" class="btn btn-outline-primary mb-3 w-100">
                            <i class="bi bi-power me-1"></i>登录后自动关闭工具：加载中...
                        </button>
                        
                        <div class="mt-3">
                            <h5 class="section-title">游戏自动启动设置</h5>
                            <div class="d-flex align-items-center mb-2">
                                <span>状态：</span>
                                <span class="ms-2 badge" id="autoStartStatus">加载中...</span>
                                <span class="ms-3">路径：</span>
                                <small id="autoStartPath" class="text-muted ms-2 text-truncate">未设置</small>
                            </div>
                            <div class="btn-group w-100">
                                <button id="setAutoStartBtn" onclick="setAutoStart(true)" class="btn btn-outline-success">
                                    <i class="bi bi-plus-circle me-1"></i>设置游戏路径
                                </button>
                                <button id="disableAutoStartBtn" onclick="setAutoStart(false)" class="btn btn-outline-danger">
                                    <i class="bi bi-dash-circle me-1"></i>禁用自启
                                </button>
                                <button id="startGameBtn" onclick="startGame()" class="btn btn-primary">
                                    <i class="bi bi-play-fill me-1"></i>立即启动游戏
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- 账号列表 -->
        <div class="card mt-4">
            <div class="card-header d-flex justify-content-between align-items-center">
                <span><i class="bi bi-list-ul me-2"></i>账号列表</span>
                <div class="btn-group" id="batchOperationsGroup">
                    <button onclick="batchDelete()" class="btn btn-danger btn-sm">
                        <i class="bi bi-trash me-1"></i>批量删除
                    </button>
                </div>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-striped table-hover">
                        <thead class="table-light">
                            <tr>
                                <th scope="col" width="5%"><input type="checkbox" id="selectAll"></th>
                                <th scope="col" width="20%">UUID</th>
                                <th scope="col" width="20%">名称</th>
                                <th scope="col" width="25%">上次登录</th>
                                <th scope="col" width="30%">操作</th>
                            </tr>
                        </thead>
                        <tbody id="channelTableBody">
                            <!-- 账号记录将在这里显示 -->
                        </tbody>
                    </table>
                </div>
                <div id="noAccounts" class="alert alert-secondary text-center" style="display:none;">
                    <i class="bi bi-info-circle me-2"></i> 暂无账号记录，请通过手动登录或用游戏客户端扫码添加账号
                </div>
            </div>
        </div>
        
        <footer class="mt-4 text-center text-muted">
            <p class="small">IDV-LOGIN渠道服账号管理界面 &copy; 2025</p>
            <p class="small">
                <a href="https://www.yuque.com/keygen/kg2r5k/izpgpf4g3ecqsbf3" target="_blank" class="text-decoration-none">官方教程</a>
            </p>
            <p class="small">
                <a href="https://www.yuque.com/keygen/kg2r5k/xl9zosrwviyc54nu" target="_blank" class="text-decoration-none">出现问题？</a>
            </p>
        </footer>
    </div>

    
    <script>
        function timeStampToLocalTime(timestamp) {
            return new Date(timestamp * 1000).toLocaleString();
        };
        function renameChannel(uuid) {
            var newName = prompt("请输入新的账号名称");
            if (newName) {
                fetch(`/_idv-login/rename?uuid=${uuid}&new_name=${newName}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            swal('账号已成功改名');
                            location.reload();
                        } else {
                            swal('改名失败');
                        }
                    });
            }
        }

        function deleteChannel(uuid) {
            swal({
                title: "确定要删除这个账号吗？",
                text: "删除后将无法恢复此账号信息",
                icon: "warning",
                buttons: ["取消", "确定删除"],
                dangerMode: true,
            })
            .then((willDelete) => {
                if (willDelete) {
                    fetch(`/_idv-login/del?uuid=${uuid}`)
                        .then(response => response.json())
                        .then(data => {
                            if (data.success) {
                                swal('账号已成功删除', {
                                    icon: "success",
                                });
                                location.reload();
                            } else {
                                swal('删除失败', {
                                    icon: "error",
                                });
                            }
                        });
                }
            });
        }

        function batchDelete() {
            // 获取所有选中的账号
            const selectedAccounts = [];
            document.querySelectorAll('.account-checkbox:checked').forEach(checkbox => {
                selectedAccounts.push(checkbox.value);
            });

            if (selectedAccounts.length === 0) {
                swal({
                    title: "未选择账号",
                    text: "请先选择要删除的账号",
                    icon: "warning",
                    button: "确定",
                });
                return;
            }

            swal({
                title: "批量删除确认",
                text: `确定要删除选中的 ${selectedAccounts.length} 个账号吗？此操作不可撤销。`,
                icon: "warning",
                buttons: ["取消", "确定删除"],
                dangerMode: true,
            })
            .then((willDelete) => {
                if (willDelete) {
                    // 显示加载中提示
                    swal({
                        title: "删除中...",
                        text: "正在批量删除账号",
                        icon: "info",
                        buttons: false,
                        closeOnClickOutside: false,
                    });

                    // 创建一个Promise数组来跟踪所有删除请求
                    const deletePromises = selectedAccounts.map(uuid => {
                        return fetch(`/_idv-login/del?uuid=${uuid}`)
                            .then(response => response.json())
                            .then(data => ({uuid, success: data.success}));
                    });

                    // 等待所有删除请求完成
                    Promise.all(deletePromises)
                        .then(results => {
                            const successful = results.filter(r => r.success).length;
                            const failed = results.length - successful;

                            swal({
                                title: "批量删除完成",
                                text: `成功删除 ${successful} 个账号${failed > 0 ? `，${failed} 个账号删除失败` : ''}`,
                                icon: successful > 0 ? "success" : "warning",
                                button: "确定",
                            }).then(() => {
                                location.reload();
                            });
                        });
                }
            });
        }

        function switchChannel(uuid) {
            fetch(`/_idv-login/switch?uuid=${uuid}`)
                .then(response => response.json())
                .then(data => {
                    if (data.current == uuid) {
                        swal({
                            title: "模拟登录成功",
                            icon: "success",
                            button: "确定",
                        });
                        location.reload();
                    } else {
                        swal({
                            title: "登录失败",
                            icon: "error",
                            button: "确定",
                        });
                    }
                });
        }
        function defaultChannel(uuid) {
            game_id = getQueryVariable("game_id");
            fetch(`/_idv-login/setDefault?uuid=${uuid}&game_id=${game_id}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        swal({
                            title: "设置默认账号成功",
                            icon: "success",
                            button: "确定",
                        });
                        location.reload();
                    } else {
                        swal({
                            title: "设置失败",
                            icon: "error",
                            button: "确定",
                        });
                    }
                });
        }

        function clearDefault() {
            game_id = getQueryVariable("game_id");
            fetch(`/_idv-login/clearDefault?game_id=${game_id}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        swal({
                            title: "清除默认账号成功",
                            icon: "success",
                            button: "确定",
                        });
                        location.reload();
                    } else {
                        swal({
                            title: "清除失败",
                            icon: "error",
                            button: "确定",
                        });
                    }
                });
        }

        // 登录延迟相关函数
        function loadLoginDelay() {
            const game_id = getQueryVariable("game_id");
            if (!game_id) return;
            
            fetch(`/_idv-login/get-login-delay?game_id=${game_id}`)
                .then(response => response.json())
                .then(data => {
                    document.getElementById('currentDelay').textContent = data.delay;
                    document.getElementById('loginDelayInput').value = data.delay;
                })
                .catch(err => {
                    console.error('获取登录延迟失败:', err);
                    document.getElementById('currentDelay').textContent = '获取失败';
                });
        }

        function saveLoginDelay() {
            const game_id = getQueryVariable("game_id");
            const delay = document.getElementById('loginDelayInput').value;
            
            if (!game_id) {
                swal({
                    title: "错误",
                    text: "未找到游戏ID",
                    icon: "error",
                    button: "确定",
                });
                return;
            }
            
            if (delay === "" || isNaN(delay) || delay < 0) {
                swal({
                    title: "输入错误",
                    text: "请输入有效的延迟时间（秒）",
                    icon: "warning",
                    button: "确定",
                });
                return;
            }
            
            fetch(`/_idv-login/set-login-delay?game_id=${game_id}&delay=${delay}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        swal({
                            title: "设置成功",
                            text: `登录延迟已设置为 ${delay} 秒`,
                            icon: "success",
                            button: "确定",
                        });
                        document.getElementById('currentDelay').textContent = delay;
                    } else {
                        swal({
                            title: "设置失败",
                            text: data.error || "未知错误",
                            icon: "error",
                            button: "确定",
                        });
                    }
                })
                .catch(err => {
                    swal({
                        title: "请求失败",
                        text: "保存延迟设置时发生错误",
                        icon: "error",
                        button: "确定",
                    });
                });
        }

        function getQueryVariable(variable) {
            var query = window.location.search.substring(1);
            var vars = query.split("&");
            for (var i = 0; i < vars.length; i++) {
                var pair = vars[i].split("=");
                if (pair[0] == variable) { return pair[1]; }
            }
            return ("");
        }

        function cmpGameId(game_id_a, game_id_b) {
            if (game_id_a === "" || game_id_b === "") {
                return game_id_a === game_id_b;
            }
            const short_a = game_id_a.split("-").pop();
            const short_b = game_id_b.split("-").pop();
            return short_a === short_b;
        }

        // 游戏切换函数
        function switchGame(gameId) {
            if (gameId) {
                // 显示加载中提示
                swal({
                    title: "切换游戏中...",
                    text: "正在加载游戏数据",
                    icon: "info",
                    buttons: false,
                    closeOnClickOutside: false,
                });
                
                // 重定向到相同的URL，但改变game_id查询参数
                const currentUrl = new URL(window.location.href);
                currentUrl.searchParams.set('game_id', gameId);
                window.location.href = currentUrl.toString();
            }
        }
        
        // 加载游戏列表
        function loadGameList() {
            fetch('/_idv-login/list-games')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        const gameSelect = document.getElementById('gameSelect');
                        // 清空现有选项
                        gameSelect.innerHTML = '';
                        
                        // 获取当前选中的游戏ID
                        const currentGameId = getQueryVariable("game_id");
                        
                        // 添加游戏选项
                        data.games.forEach(game => {
                            const option = document.createElement('option');
                            option.value = game.game_id;
                            option.text = game.name || game.game_id;
                            option.selected = cmpGameId(game.game_id, currentGameId);
                            gameSelect.appendChild(option);
                        });

                        // 如果没有当前游戏ID或列表中没有此游戏
                        if (!currentGameId && data.games.length > 0) {
                            switchGame(data.games[0].game_id);
                        }
                        //如果当前有id但是列表中没有，展示当前id
                        has_match=false;
                        for (var i = 0; i < data.games.length; i++) {
                            if (cmpGameId(data.games[i].game_id, currentGameId)) {
                                has_match=true;
                                break;
                            }
                        }
                        if (!has_match) {
                            option = document.createElement('option');
                            option.value = currentGameId;
                            option.text = currentGameId;
                            option.selected = true;
                            gameSelect.appendChild(option);
                        }
                    } else {
                        console.error('获取游戏列表失败');
                        document.getElementById('gameSelect').innerHTML = '<option value="">加载失败</option>';
                    }
                });
        }
        
        // 自动启动相关函数
        function loadAutoStartSettings() {
            const game_id = getQueryVariable("game_id");
            fetch(`/_idv-login/get-game-auto-start?game_id=${game_id}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        updateAutoStartUI(data.enabled, data.path);
                    } else {
                        swal({
                            title: "获取自动启动设置失败",
                            icon: "error",
                            button: "确定",
                        });
                    }
                });
        }
        
        function updateAutoStartUI(enabled, path) {
            const statusElement = document.getElementById('autoStartStatus');
            const pathElement = document.getElementById('autoStartPath');
            const setBtn = document.getElementById('setAutoStartBtn');
            const disableBtn = document.getElementById('disableAutoStartBtn');
            const startBtn = document.getElementById('startGameBtn');
            
            statusElement.textContent = enabled ? '已启用' : '已禁用';
            statusElement.className = enabled ? 'ms-2 badge bg-success' : 'ms-2 badge bg-secondary';
            
            if (path) {
                pathElement.textContent = path;
                pathElement.title = path; // 添加完整路径作为提示
            } else {
                pathElement.textContent = '未设置';
                pathElement.title = '';
            }
            
            setBtn.disabled = enabled;
            disableBtn.disabled = !enabled;
            startBtn.disabled = !enabled || !path;
        }
        
        function setAutoStart(enabled) {
            const game_id = getQueryVariable("game_id");
            fetch(`/_idv-login/set-game-auto-start?game_id=${game_id}&enabled=${enabled}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        updateAutoStartUI(data.enabled, data.path);
                        if (enabled) {
                            if (data.path) {
                                swal({
                                    title: "设置成功",
                                    text: "已成功设置游戏自动启动",
                                    icon: "success",
                                    button: "确定",
                                });
                            } else {
                                swal({
                                    title: "设置已取消",
                                    text: "取消了游戏路径选择，未启用自动启动",
                                    icon: "info",
                                    button: "确定",
                                });
                            }
                        } else {
                            swal({
                                title: "已禁用",
                                text: "已禁用游戏自动启动功能",
                                icon: "info",
                                button: "确定",
                            });
                        }
                    } else {
                        swal({
                            title: "设置失败",
                            text: data.error || '未知错误',
                            icon: "error",
                            button: "确定",
                        });
                    }
                });
        }
        
        function startGame() {
            const game_id = getQueryVariable("game_id");
            
            swal({
                title: "正在启动游戏",
                text: "请稍候...",
                icon: "info",
                buttons: false,
                closeOnClickOutside: false,
            });
            
            fetch(`/_idv-login/start-game?game_id=${game_id}`)
                .then(response => response.json())
                .then(data => {
                    swal.close();
                    if (data.success) {
                        swal({
                            title: "游戏启动中",
                            text: "如果游戏没有自动启动，请检查游戏路径是否正确",
                            icon: "success",
                            button: "确定",
                        });
                    } else {
                        swal({
                            title: "启动失败",
                            text: data.error || '未知错误',
                            icon: "error",
                            button: "确定",
                        });
                    }
                });
        }

        // 在页面加载时获取账号列表
        window.onload = function () {
            //获得query参数game_id
            game_id = getQueryVariable("game_id");
            
            // 首先加载游戏列表
            loadGameList();
            
            // 如果有game_id，加载该游戏的账号和设置
            if (game_id) {
                // 加载登录延迟设置
                loadLoginDelay();
                
                fetch('/_idv-login/list?game_id=' + game_id)
                    .then(response => response.json())
                    .then(data => {
                        var tableBody = document.getElementById('channelTableBody');
                        var noAccountsDiv = document.getElementById('noAccounts');
                        var batchOperationsGroup = document.getElementById('batchOperationsGroup');
                        
                        // 清空表格
                        tableBody.innerHTML = '';
                        
                        if (data.length === 0) {
                            noAccountsDiv.style.display = 'block';
                            batchOperationsGroup.style.display = 'none'; // 没有账号时隐藏批量操作
                        } else {
                            noAccountsDiv.style.display = 'none';
                            batchOperationsGroup.style.display = 'block'; // 有账号时显示批量操作
                            
                            data.forEach(channel => {
                                var row = tableBody.insertRow();
                                row.insertCell().innerHTML = `<input type="checkbox" class="account-checkbox" value="${channel.uuid}">`;
                                
                                var uuidCell = row.insertCell();
                                uuidCell.innerHTML = `<span class="badge bg-light text-dark">${channel.uuid}</span>`;
                                uuidCell.title = channel.uuid;
                                
                                row.insertCell().innerHTML = channel.name;
                                row.insertCell().innerHTML = `<small>${timeStampToLocalTime(channel.last_login_time)}</small>`;
                                
                                var actionsCell = row.insertCell();
                                actionsCell.innerHTML = `
                                    <div class="actions-container">
                                        <div class="action-button">
                                            <button onclick="switchChannel('${channel.uuid}')" class="btn btn-sm btn-primary btn-action">
                                                <i class="bi bi-box-arrow-in-right"></i>
                                            </button>
                                            <span class="action-label">登录</span>
                                        </div>
                                        <div class="action-button">
                                            <button onclick="renameChannel('${channel.uuid}')" class="btn btn-sm btn-info btn-action text-white">
                                                <i class="bi bi-pencil"></i>
                                            </button>
                                            <span class="action-label">改名</span>
                                        </div>
                                        <div class="action-button">
                                            <button onclick="deleteChannel('${channel.uuid}')" class="btn btn-sm btn-danger btn-action">
                                                <i class="bi bi-trash"></i>
                                            </button>
                                            <span class="action-label">删除</span>
                                        </div>
                                        <div class="action-button">
                                            <button onclick="defaultChannel('${channel.uuid}')" class="btn btn-sm btn-success btn-action">
                                                <i class="bi bi-star"></i>
                                            </button>
                                            <span class="action-label">默认</span>
                                        </div>
                                    </div>
                                `;
                            });
                            
                            // 全选功能
                            document.getElementById('selectAll').addEventListener('change', function() {
                                const checkboxes = document.querySelectorAll('.account-checkbox');
                                checkboxes.forEach(checkbox => {
                                    checkbox.checked = this.checked;
                                });
                            });
                        }
                    });

                fetch('/_idv-login/manualChannels')
                    .then(response => response.json())
                    .then(data => {
                        var channelSelect = document.getElementById('channelSelect');
                        // 清空下拉框
                        channelSelect.innerHTML = '';
                        
                        data.forEach(channel => {
                            var option = document.createElement('option');
                            option.value = channel.channel;
                            option.text = channel.name;
                            channelSelect.appendChild(option);
                        });
                    });

                fetch('/_idv-login/defaultChannel?game_id=' + game_id)
                    .then(response => response.json())
                    .then(data => {
                        if (data.uuid != "") {
                            document.getElementById('default').innerText = data.uuid;
                            document.getElementById('default').className = 'badge bg-success text-white p-2';
                        } else {
                            document.getElementById('default').innerText = 'Empty';
                            document.getElementById('default').className = 'badge bg-info text-white p-2';
                        }
                    });

                // 获取自动关闭状态
                fetch(`/_idv-login/get-auto-close-state?game_id=${game_id}`)
                    .then(response => response.json())
                    .then(data => {
                        if(data.success) {
                            updateAutoCloseButton(data.state);
                        }
                    });
                    
                // 加载自动启动设置
                loadAutoStartSettings();
            }
        }

        // 添加新的函数
        function updateAutoCloseButton(state) {
            const btn = document.getElementById('autoCloseBtn');
            if (state) {
                btn.innerHTML = `<i class="bi bi-power me-1"></i>登录后自动关闭工具：开启`;
                btn.className = 'btn btn-success w-100';
            } else {
                btn.innerHTML = `<i class="bi bi-power me-1"></i>登录后自动关闭工具：关闭`;
                btn.className = 'btn btn-outline-secondary w-100';
            }
        }

        function switchAutoClose() {
            game_id = getQueryVariable("game_id");
            fetch(`/_idv-login/switch-auto-close-state?game_id=${game_id}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        updateAutoCloseButton(data.state);
                        swal({
                            title: data.state ? "自动关闭已开启" : "自动关闭已关闭",
                            text: data.state ? "登录成功后将自动关闭工具" : "登录后工具将保持运行",
                            icon: "success",
                            button: "确定",
                        });
                    } else {
                        swal({
                            title: "切换状态失败",
                            icon: "error",
                            button: "确定",
                        });
                    }
                });
        }
        function manual() {
            //获取channelSelect的值
            var selectedChannel = document.getElementById('channelSelect').value;
            
            if (!selectedChannel) {
                swal({
                    title: "请选择登录渠道",
                    icon: "warning",
                    button: "确定",
                });
                return;
            }
            
            swal({
                title: "正在处理",
                text: "正在执行手动登录，请稍候...如果在此界面停留过久，请参考教程文档->常见问题解决方案问题18",
                icon: "info",
                buttons: false,
                closeOnClickOutside: false,
            });
            
            fetch(`/_idv-login/import?channel=${selectedChannel}&game_id=${game_id}`)
                .then(response => response.json())
                .then(data => {
                    swal.close();
                    if (data.success) {
                        swal({
                            title: "执行成功",
                            text: "登录操作已完成",
                            icon: "success",
                            button: "确定",
                        }).then(() => {
                            location.reload();
                        });
                    } else {
                        swal({
                            title: "执行失败",
                            text: "请检查工具日志获取详细信息",
                            icon: "error",
                            button: "确定",
                        });
                    }
                });
        }
    </script>
</body>

</html>"""