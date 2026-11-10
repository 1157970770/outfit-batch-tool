import { useEffect, useState, type FormEvent } from 'react';

import { errorText } from '@/api';
import { login, useSession } from '@/session';
import { toast } from '@/ui/toast';

export function BootView({ message }: { message?: string }) {
  return (
    <section id="boot-view" className="view active-view boot-view">
      <div className="boot-card">
        <div className="boot-spinner" aria-hidden="true" />
        <p>{message || '正在连接服务…'}</p>
      </div>
    </section>
  );
}

export function LoginView({ hint }: { hint?: string }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(hint || '');

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!username.trim() || !password) {
      setError('请填写账号和密码。');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await login(username.trim(), password);
      toast('登录成功', 'success');
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section id="login-view" className="view active-view login-view">
      <form className="login-card" onSubmit={submit}>
        <h1>换装批量生产器</h1>
        <p className="login-sub">妙搭版 · 请使用局域网账号登录</p>
        <label className="field">
          <span>账号</span>
          <input
            name="username"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={busy}
            autoFocus
          />
        </label>
        <label className="field">
          <span>密码</span>
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={busy}
          />
        </label>
        {error ? <p className="login-error">{error}</p> : null}
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? '登录中…' : '登录'}
        </button>
      </form>
    </section>
  );
}

/** 首屏探活：只在挂载后跑一次，避免 StrictMode 双调用重复请求。 */
export function useBootOnce(): void {
  const { status } = useSession();
  useEffect(() => {
    void status;
  }, [status]);
}
