// 引入Node.js内置模块
const http = require('http');
const { parse } = require('querystring');
const fs = require('fs');
const path = require('path');

// 端口配置
const PORT = 8700;

// 创建服务器
const server = http.createServer(async (req, res) => {
  // ========== 全局配置：允许跨域 + 统一响应格式 ==========
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,DELETE,PATCH,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', '*');
  res.setHeader('Content-Type', 'application/json; charset=utf-8');

  // 处理跨域预检请求 OPTIONS
  if (req.method === 'OPTIONS') {
    res.writeHead(200);
    res.end();
    return;
  }

  // ========== 工具函数：解析请求体数据 ==========
  const getRequestBody = () => {
    return new Promise((resolve) => {
      let body = '';
      req.on('data', (chunk) => (body += chunk));
      req.on('end', () => resolve(body));
    });
  };

  // ========== 路由分发：根据 URL + 请求方法 处理不同测试场景 ==========
  const { url, method, headers } = req;
  const [pathUrl] = url.split('?'); // 拆分路径和查询参数
  const response = {
    code: 200,
    message: '请求成功',
    requestInfo: {
      method,
      url: pathUrl,
      queryParams: parse(url.split('?')[1] || ''), // URL查询参数(Params)
      headers, // 请求头
      cookies: headers.cookie || '', // Cookie
    },
    data: null,
  };

  try {
    // ======================================
    // 1. 测试 GET 请求 + URL查询参数 + 请求头
    // ======================================
    if (pathUrl === '/test/get' && method === 'GET') {
      response.data = {
        desc: 'GET请求测试成功',
        receiveParams: response.requestInfo.queryParams,
      };
    }

    // ======================================
    // 2. 测试 POST 请求（JSON / 表单格式）
    // ======================================
    else if (pathUrl === '/test/post' && method === 'POST') {
      const body = await getRequestBody();
      response.requestInfo.body = headers['content-type']?.includes('json')
        ? JSON.parse(body)
        : parse(body);
      response.data = { desc: 'POST请求测试成功', receiveBody: response.requestInfo.body };
    }

    // ======================================
    // 3. 测试 PUT 请求（修改数据）
    // ======================================
    else if (pathUrl === '/test/put' && method === 'PUT') {
      response.data = { desc: 'PUT请求测试成功，数据已修改' };
    }

    // ======================================
    // 4. 测试 DELETE 请求（删除数据）
    // ======================================
    else if (pathUrl === '/test/delete' && method === 'DELETE') {
      response.data = { desc: 'DELETE请求测试成功，数据已删除' };
    }

    // ======================================
    // 5. 测试 PATCH 请求（局部修改）
    // ======================================
    else if (pathUrl === '/test/patch' && method === 'PATCH') {
      response.data = { desc: 'PATCH请求测试成功，局部数据已更新' };
    }

    // ======================================
    // 6. 测试 文件上传 (form-data)
    // ======================================
    else if (pathUrl === '/test/upload' && method === 'POST') {
      response.data = {
        desc: '文件上传测试成功',
        receiveFileType: headers['content-type'],
        tip: '服务器已接收文件流'
      };
    }

    // ======================================
    // 7. 测试 Bearer Token 身份认证
    // ======================================
    else if (pathUrl === '/test/auth/bearer' && method === 'GET') {
      const token = headers.authorization || '';
      if (!token.includes('Bearer ')) {
        res.writeHead(401);
        response.code = 401;
        response.message = '未授权：请携带Bearer Token';
      } else {
        response.data = { desc: 'Bearer认证成功', yourToken: token };
      }
    }

    // ======================================
    // 8. 测试 Cookie 读写
    // ======================================
    else if (pathUrl === '/test/cookie' && method === 'GET') {
      res.setHeader('Set-Cookie', 'test_cookie=123456; Path=/');
      response.data = { desc: 'Cookie测试成功', serverSetCookie: 'test_cookie=123456' };
    }

    // ======================================
    // 9. 测试 404 错误状态码
    // ======================================
    else if (pathUrl === '/test/error/404') {
      res.writeHead(404);
      response.code = 404;
      response.message = '资源不存在';
    }

    // ======================================
    // 10. 测试 500 服务器错误
    // ======================================
    else if (pathUrl === '/test/error/500') {
      res.writeHead(500);
      response.code = 500;
      response.message = '服务器内部错误';
    }

    // 默认接口
    else {
      response.data = { desc: '默认接口，欢迎测试！' };
    }

  } catch (err) {
    res.writeHead(400);
    response.code = 400;
    response.message = '请求参数错误：' + err.message;
  }

  // 返回响应结果
  res.end(JSON.stringify(response, null, 2));
});

// 启动服务器
server.listen(PORT, () => {
  console.log(`✅ 全能测试服务器已启动：http://127.0.0.1:${PORT}`);
  console.log(`📌 所有测试接口已就绪，可完整测试你的Python Postman工具`);
});