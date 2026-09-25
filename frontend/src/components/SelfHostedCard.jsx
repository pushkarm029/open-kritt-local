import './localReview.css';
import { useEffect, useState } from 'react';
import { api, apiErrorMessages } from '../api/client.js';
import { Button } from './ui.jsx';

export default function SelfHostedCard({ provider, onToggleActive, updatingActive, onSaved }) {
  const [config, setConfig] = useState({ baseUrl: '', model: '' });
  const [credential, setCredential] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState(null);
  const [check, setCheck] = useState(null);
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let active = true;
    api
      .selfHostedConfig()
      .then((value) => {
        if (active) {
          setConfig(value);
          setLoaded(true);
        }
      })
      .catch((next) => {
        if (active) setError(next);
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    if (check?.status !== 'pending') return;
    let active = true;
    const deadline = Date.now() + 90_000;
    const timer = setInterval(async () => {
      if (Date.now() >= deadline) {
        if (active)
          setCheck({ status: 'failed', error: 'The engine did not finish the check. Verify it is running and retry.' });
        return;
      }
      try {
        const result = await api.selfHostedCheck(check.id);
        if (active) setCheck(result);
      } catch (next) {
        if (active) {
          setError(next);
          setCheck(null);
        }
      }
    }, 1500);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [check?.id, check?.status]);
  const save = async (event) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    setCheck(null);
    try {
      const next = await api.saveSelfHostedConfig({ baseUrl: config.baseUrl, model: config.model });
      if (credential) {
        await api.saveProviderCredential('self_hosted', credential);
        setCredential('');
      }
      setConfig(next);
      setSaved(true);
      onSaved?.();
    } catch (next) {
      setError(next);
    } finally {
      setBusy(false);
    }
  };
  const testConnection = async () => {
    setBusy(true);
    setError(null);
    try {
      setCheck(await api.checkSelfHosted());
    } catch (next) {
      setError(next);
    } finally {
      setBusy(false);
    }
  };
  const update = (patch) => {
    setConfig((current) => ({ ...current, ...patch }));
    setSaved(false);
    setCheck(null);
  };
  return (
    <section className="account-provider-card self-hosted-card" data-provider="self_hosted">
      <div className="account-provider-header">
        <h2 style={{ fontSize: 18, margin: 0 }}>Self-hosted AI</h2>
      </div>
      <p>Default model for two-commit source reviews.</p>
      {error && (
        <div role="alert">
          {apiErrorMessages(error).map((message) => (
            <p key={message}>{message}</p>
          ))}
        </div>
      )}
      <form onSubmit={save} style={{ display: 'grid', gap: 12 }}>
        <label style={{ display: 'grid', gap: 5 }}>
          API base URL
          <input
            type="url"
            value={config.baseUrl}
            onChange={(event) => update({ baseUrl: event.target.value })}
            placeholder="https://model-host/v1"
            required
            disabled={busy || !loaded}
            autoComplete="off"
          />
        </label>
        <label style={{ display: 'grid', gap: 5 }}>
          Model ID
          <input
            value={config.model}
            onChange={(event) => update({ model: event.target.value })}
            required
            disabled={busy || !loaded}
            autoComplete="off"
          />
        </label>
        <label style={{ display: 'grid', gap: 5 }}>
          API key
          <input
            type="password"
            value={credential}
            onChange={(event) => {
              setCredential(event.target.value);
              setSaved(false);
              setCheck(null);
            }}
            placeholder={provider.configured ? 'Leave blank to keep the saved key' : 'Enter API key'}
            disabled={busy || !loaded}
            autoComplete="new-password"
          />
        </label>
        <div>
          <Button type="submit" disabled={busy || !loaded}>
            {busy ? 'Working...' : 'Save configuration'}
          </Button>{' '}
          <Button
            variant="ghost"
            disabled={busy || !loaded || !provider.configured || check?.status === 'pending'}
            onClick={testConnection}
          >
            Test saved connection
          </Button>
        </div>
      </form>
      {provider.accounts?.map((account) => (
        <label key={account.id} style={{ display: 'flex', gap: 8, marginTop: 14 }}>
          <input
            type="checkbox"
            checked={account.active}
            disabled={updatingActive || busy}
            onChange={() => onToggleActive(account)}
          />
          Active
        </label>
      ))}
      <div role="status" aria-live="polite">
        {saved && <p>Configuration saved.</p>}
        {check?.status === 'pending' && <p>Checking from the engine...</p>}
        {check?.status === 'passed' && <p>Connection and response format verified.</p>}
        {check?.status === 'failed' && <p>{check.error || 'Connection check failed.'}</p>}
      </div>
    </section>
  );
}
