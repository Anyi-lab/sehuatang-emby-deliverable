# -*- coding: utf-8 -*-
"""
global_login_115.py — 扫码全局登录 115
================================================================
一次扫码完成 115 登录并全局分发凭证:
  1. 生成网页扫码二维码 (app="web") -> PNG
  2. 用户用 115 App 扫码确认
  3. 拿到新网页 cookie (UID/CID/SEID/KID)  -> 离线推送用
  4. 用新 cookie login_another_open() 换新 open token (app=100195125,
     与 SmartStrm 的 100197651 隔离, 不会把它踢下线) -> 文件操作/上传用
  5. 全局分发:
     - 本机 115push/cookies.json + creds.json + cookies.txt (5081/刮削器/推送器)
     - 108 search-api 容器 /opt/media/search-api/data/cookies.json + creds.json
  6. 验证: open token fs_files / cookie space sign / webapi files
================================================================
"""
import json, os, sys, time, ssl, urllib.request, re

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

BASE = os.path.dirname(os.path.abspath(__file__))
COOKIES_PATH = os.path.join(BASE, 'cookies.json')
CREDS_PATH = os.path.join(BASE, 'creds.json')
COOKIES_TXT = os.path.join(BASE, 'cookies.txt')
QR_PNG = os.path.join(BASE, 'qrcode_login_global.png')
USER_ID = '<115_UID>'

# 108 search-api 容器凭证(通过 SSH 分发)
REMOTE_COOKIES = '/opt/media/search-api/data/cookies.json'
REMOTE_CREDS = '/opt/media/search-api/data/creds.json'
REMOTE_HOST = '<LAN_IP>'
REMOTE_USER = 'root'
REMOTE_PASS = '<CD2_PASSWORD>'

# 全局登录状态(供 web 端查询)
LOGIN_STATE = {'status': 'idle', 'msg': '', 'updated_at': ''}


# ----------------------------- 二维码登录 -----------------------------
def qr_login():
    """生成二维码 PNG, 返回 (uid, png_path); 失败抛异常"""
    from p115client import P115Client
    client = P115Client(cookies={})
    token = client.login_qrcode_token(app="web")
    uid = token.get('uid')
    try:
        png = client.login_qrcode(token, app="web")
        if len(png) < 500:
            raise ValueError(f'PNG too small ({len(png)}B)')
        with open(QR_PNG, 'wb') as f:
            f.write(png)
    except Exception as e:
        # 备用: 用 qrcode 库从扫码内容生成大图(扫码更可靠)
        import qrcode
        qr_data = token.get('qrcode') or token.get('url') or json.dumps(token)
        img = qrcode.make(qr_data)
        img = img.resize((300, 300))
        img.save(QR_PNG)
    return uid, QR_PNG


def wait_scan(uid, timeout=180, on_status=None):
    """轮询扫码结果, 返回 cookies dict {UID,CID,SEID,KID,...}; 超时抛异常"""
    from p115client import P115Client
    client = P115Client(cookies={})
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = client.login_qrcode_scan_result(uid, app="web")
        data = r.get('data') or {}
        cookies = data.get('cookies') or {}
        if cookies:
            keep = {}
            for k in ['UID', 'CID', 'SEID', 'KID', 'time', 'login_type', 'deviceID']:
                if k in cookies:
                    keep[k] = cookies[k]
            if 'UID' in keep:
                return keep
        if on_status:
            on_status(f'等待扫码... state={r.get("state")} ({int(time.time()-t0)}s)')
        time.sleep(3)
    raise TimeoutError('扫码超时(180s)')


# ----------------------------- open token -----------------------------
def exchange_open(cookies):
    """用网页 cookie 换独立 open token (app=100195125, 与 SmartStrm 隔离)"""
    from p115client import P115Client
    c = P115Client(cookies=cookies)
    oc = c.login_another_open()  # default app_id -> 100195125
    oc.headers['authorization'] = 'Bearer ' + oc.access_token
    r = oc.fs_files(0)
    if not r.get('state'):
        raise RuntimeError('新 open token fs_files 校验失败: ' + json.dumps(r, ensure_ascii=False)[:200])
    return {
        'access_token': oc.access_token,
        'refresh_token': oc.refresh_token,
        'user_id': USER_ID,
        'app_id': oc.app_id,
    }


# ----------------------------- 验证 -----------------------------
def _cookie_str(cookies):
    return '; '.join(f'{k}={v}' for k, v in cookies.items())


def verify(cookies, creds):
    """验证 cookie(space sign + webapi files) 和 open token(fs_files)"""
    h = {
        'Cookie': _cookie_str(cookies),
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/62.0.3202.94 Safari/537.36 115Browser/9.1.1',
        'Origin': 'https://115.com', 'Referer': 'https://115.com/',
        'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json, text/javascript, */*; q=0.01',
    }
    # space sign
    req = urllib.request.Request('https://115.com/?ct=offline&ac=space', headers=h)
    with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
        sp = json.loads(resp.read().decode('utf-8', 'replace'))
    if not sp.get('sign'):
        raise RuntimeError('cookie space sign 失败: ' + json.dumps(sp, ensure_ascii=False)[:150])
    # webapi files
    req = urllib.request.Request('https://webapi.115.com/files?aid=1&cid=0&o=user_ptime&asc=0&offset=0&show_dir=1&limit=3&format=json', headers=h)
    with urllib.request.urlopen(req, timeout=25, context=ctx) as resp:
        wf = json.loads(resp.read().decode('utf-8', 'replace'))
    if 'count' not in wf:
        raise RuntimeError('cookie webapi files 失败: ' + json.dumps(wf, ensure_ascii=False)[:150])
    # open token
    from p115client import P115OpenClient
    oc = P115OpenClient(access_token=creds['access_token'], refresh_token=creds['refresh_token'],
                        app_id=creds.get('app_id', 100195125), console_qrcode=False)
    oc.headers['authorization'] = 'Bearer ' + creds['access_token']
    r = oc.fs_files(0)
    if not r.get('state'):
        raise RuntimeError('open token fs_files 失败: ' + json.dumps(r, ensure_ascii=False)[:150])
    return True


# ----------------------------- 分发 -----------------------------
def _write_local(cookies, creds):
    with open(COOKIES_PATH, 'w', encoding='utf-8') as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)
    with open(CREDS_PATH, 'w', encoding='utf-8') as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
    with open(COOKIES_TXT, 'w', encoding='utf-8') as f:
        f.write(_cookie_str(cookies) + ';')
    return True


def _write_remote(cookies, creds):
    """通过 paramiko SFTP 分发到 108 search-api 容器 data 目录"""
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(REMOTE_HOST, username=REMOTE_USER, password=REMOTE_PASS, timeout=10)
    sftp = cli.open_sftp()
    with sftp.open(REMOTE_COOKIES, 'wb') as f:
        f.write(json.dumps(cookies, ensure_ascii=False, indent=2).encode('utf-8'))
    with sftp.open(REMOTE_CREDS, 'wb') as f:
        f.write(json.dumps(creds, ensure_ascii=False, indent=2).encode('utf-8'))
    sftp.close()
    cli.close()
    return True


def distribute(cookies, creds):
    """全局分发凭证, 返回各目标结果"""
    results = {}
    results['local_cookies'] = _write_local(cookies, creds)
    try:
        results['remote_108'] = _write_remote(cookies, creds)
    except Exception as e:
        results['remote_108'] = 'FAIL: ' + str(e)[:150]
    return results


# ----------------------------- 完整流程 -----------------------------
def do_global_login(timeout=180, on_status=None):
    """执行完整扫码全局登录; 返回汇总 dict"""
    def status(msg):
        LOGIN_STATE['status'] = 'running'
        LOGIN_STATE['msg'] = msg
        LOGIN_STATE['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        if on_status:
            on_status(msg)

    try:
        status('生成二维码...')
        uid, png = qr_login()
        LOGIN_STATE['qr_png'] = png
        LOGIN_STATE['uid'] = uid
        status('请用 115 App 扫码确认')
        cookies = wait_scan(uid, timeout=timeout, on_status=status)
        status('扫码成功, 换取 open token...')
        creds = exchange_open(cookies)
        status('验证凭证...')
        verify(cookies, creds)
        status('全局分发凭证...')
        dist = distribute(cookies, creds)
        LOGIN_STATE['status'] = 'done'
        LOGIN_STATE['msg'] = '✅ 全局登录成功, 凭证已分发到本机 + 108 search-api'
        LOGIN_STATE['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        return {'status': 'done', 'uid': uid, 'cookies_keys': list(cookies.keys()),
                'app_id': creds.get('app_id'), 'distribute': dist,
                'png': png}
    except Exception as e:
        LOGIN_STATE['status'] = 'failed'
        LOGIN_STATE['msg'] = '❌ ' + str(e)[:200]
        LOGIN_STATE['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        return {'status': 'failed', 'error': str(e)[:300]}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='115 扫码全局登录')
    ap.add_argument('--timeout', type=int, default=180, help='扫码超时秒数')
    args = ap.parse_args()
    r = do_global_login(timeout=args.timeout)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    if r.get('png'):
        print('二维码: ' + r['png'])
