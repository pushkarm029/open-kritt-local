import '../components/localReview.css';
import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, apiErrorMessages } from '../api/client.js';
import { Button, ErrorState, Spinner } from '../components/ui.jsx';
import Markdown from '../components/Markdown.jsx';
import { usePageChrome } from '../context/ui.jsx';
import { saveBrowserDownload } from '../lib/download.js';
import { isScanDeletable } from '../lib/scanPresentation.js';
import { useUnsavedChangesPrompt } from '../lib/useUnsavedChangesPrompt.js';

const commitId = /^[a-fA-F0-9]{7,64}$/;
const panel = { padding: '26px 32px', maxWidth: 960 };
const field = { display: 'grid', gap: 6, marginBottom: 18 };

export function commitReviewDraft(source) {
  return {
    repo_kind: source?.repoKind || 'local',
    repo_full: source?.repoFull || '',
    base_commit_sha: source?.baseCommitSha || '',
    commit_sha: source?.commitSha || '',
    comparison_mode: 'commits',
  };
}

export function CommitReviewForm({ source }) {
  const navigate = useNavigate();
  const [draft, setDraft] = useState(() => commitReviewDraft(source));
  const [config, setConfig] = useState(null);
  const [repos, setRepos] = useState([]);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [launchChoice, setLaunchChoice] = useState(false);
  const { allow } = useUnsavedChangesPrompt(dirty || busy);
  usePageChrome(
    [
      { label: 'Scans', to: '/scans' },
      { label: 'Two-commit review', active: true },
    ],
    null,
    []
  );
  useEffect(() => {
    let active = true;
    Promise.all([api.selfHostedConfig(), api.localRepos()])
      .then(([nextConfig, localRepos]) => {
        if (active) {
          setConfig(nextConfig);
          setRepos(localRepos);
        }
      })
      .catch((next) => {
        if (active) setError(next);
      });
    return () => {
      active = false;
    };
  }, []);
  const update = (patch) => {
    setDirty(true);
    setDraft((current) => ({ ...current, ...patch }));
  };
  const sourceMatches =
    !source || (source.model === config?.model && source.configuration?.self_hosted_base_url === config?.baseUrl);
  const ready = config?.configured && config?.active !== false && sourceMatches;
  const valid =
    ready && draft.repo_full.trim() && commitId.test(draft.base_commit_sha) && commitId.test(draft.commit_sha);
  const submit = async (launchPolicy) => {
    setBusy(true);
    setError(null);
    try {
      const created = await api.createScan({ ...draft, ...(launchPolicy ? { launchPolicy } : {}) });
      allow();
      navigate(`/scans/${created.id}`);
    } catch (next) {
      if (next.code === 'scan_launch_policy_required') setLaunchChoice(true);
      else setError(next);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="commit-review" style={panel}>
      <h1 style={{ fontSize: 24 }}>Review two commits</h1>
      <p style={{ color: 'var(--text-2)' }}>
        Compare the base tree with the head tree. Only committed source is included.
      </p>
      {source && (
        <p>
          Configuration copied from <Link to={`/scans/${source.id}`}>scan {source.id}</Link>.
        </p>
      )}
      {error && (
        <div role="alert">
          {apiErrorMessages(error).map((message) => (
            <p key={message}>{message}</p>
          ))}
        </div>
      )}
      {!config && !error && <Spinner label="Loading local AI configuration" />}
      {config && (
        <p>
          Model: <strong>Self-hosted AI{config.model ? ` / ${config.model}` : ''}</strong>.{' '}
          {!ready && <Link to="/accounts">Configure and activate Self-hosted AI in Accounts.</Link>}
        </p>
      )}
      {source && config && !sourceMatches && (
        <p role="alert">
          Local AI settings changed since this review. Create a new review to use the current settings.
        </p>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (valid && !busy) submit();
        }}
      >
        <label style={field}>
          Repository source
          <select
            value={draft.repo_kind}
            onChange={(event) => update({ repo_kind: event.target.value, repo_full: '' })}
            disabled={busy}
          >
            <option value="local">Local</option>
            <option value="remote">GitHub</option>
          </select>
        </label>
        <label style={field}>
          Repository
          {draft.repo_kind === 'local' ? (
            <select
              value={draft.repo_full}
              onChange={(event) => update({ repo_full: event.target.value })}
              required
              disabled={busy}
            >
              <option value="">Select a Git repository</option>
              {repos
                .filter((repo) => repo.supportsCommitReview)
                .map((repo) => (
                  <option key={repo.name} value={repo.name}>
                    {repo.name}
                  </option>
                ))}
            </select>
          ) : (
            <input
              value={draft.repo_full}
              onChange={(event) => update({ repo_full: event.target.value })}
              placeholder="owner/repository"
              required
              disabled={busy}
            />
          )}
        </label>
        {draft.repo_kind === 'local' && repos.some((repo) => repo.isGit && !repo.supportsCommitReview) && (
          <p>Linked worktrees are not supported. Select the main repository.</p>
        )}
        <CommitFields draft={draft} onChange={update} disabled={busy} />
        <Button disabled={!valid || busy} type="submit">
          {busy ? 'Starting review...' : 'Start review'}
        </Button>
      </form>
      {launchChoice && (
        <div role="group" aria-label="Scan launch choice" style={{ marginTop: 20 }}>
          <p>A scan is already running. Start this review now or queue it.</p>
          <Button disabled={busy} onClick={() => submit('immediate')}>
            Start immediately
          </Button>{' '}
          <Button disabled={busy} onClick={() => submit('queue')}>
            Queue
          </Button>
        </div>
      )}
    </div>
  );
}

export function CommitFields({ draft, onChange, disabled }) {
  return (
    <>
      {[
        ['base_commit_sha', 'Base commit'],
        ['commit_sha', 'Head commit'],
      ].map(([key, label]) => (
        <label key={key} style={field}>
          {label}
          <input
            className="mono"
            value={draft[key]}
            onChange={(event) => onChange({ [key]: event.target.value.trim() })}
            pattern="[a-fA-F0-9]{7,64}"
            title="Enter a full or unambiguous abbreviated commit ID, at least 7 hexadecimal characters."
            required
            disabled={disabled}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
      ))}
    </>
  );
}

export function CommitReviewResults({ scan, reload }) {
  const navigate = useNavigate();
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const review = scan.diffReview;
  const changeStatus = async (status) => {
    setBusy(true);
    setError(null);
    try {
      await api.updateScanStatus(scan.id, status);
      reload();
    } catch (next) {
      setError(next);
    } finally {
      setBusy(false);
    }
  };
  const exportReview = async () => {
    setBusy(true);
    setError(null);
    try {
      saveBrowserDownload(await api.exportScanFindings(scan.id));
    } catch (next) {
      setError(next);
    } finally {
      setBusy(false);
    }
  };
  const deleteReview = async () => {
    if (!window.confirm('Permanently delete this review and its results?')) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteScan(scan.id);
      navigate('/scans', { replace: true });
    } catch (next) {
      setError(next);
      setBusy(false);
    }
  };
  return (
    <div className="commit-review" style={panel}>
      <h1 style={{ fontSize: 24 }}>{scan.repoDisplay || scan.repoFull}</h1>
      <p>
        Two-commit review · {scan.status} · {scan.model}
      </p>
      <dl>
        <dt>Base commit</dt>
        <dd className="mono">{scan.baseCommitSha}</dd>
        <dt>Head commit</dt>
        <dd className="mono">{scan.commitSha}</dd>
      </dl>
      <div style={{ display: 'flex', gap: 12, margin: '18px 0' }}>
        <Link to={`/scans/new?from=${scan.id}`}>Duplicate configuration</Link>
        {['completed', 'failed', 'stopped'].includes(scan.status) && (
          <Button disabled={busy} onClick={exportReview}>
            Export review
          </Button>
        )}
        {isScanDeletable(scan.status) && (
          <Button variant="ghost" disabled={busy} onClick={deleteReview}>
            Delete
          </Button>
        )}
        {['running', 'pending', 'queued'].includes(scan.status) && (
          <Button disabled={busy} onClick={() => changeStatus('stopped')}>
            Stop
          </Button>
        )}
        {['failed', 'stopped', 'paused'].includes(scan.status) && (
          <Button disabled={busy} onClick={() => changeStatus('pending')}>
            Retry
          </Button>
        )}
      </div>
      {error && <ErrorState error={error} />}
      {scan.reasoning?.errors?.length
        ? scan.reasoning.errors.map((item) => (
            <p key={item.field} role="alert">
              {{ base_commit_sha: 'Base commit', commit_sha: 'Head commit', repo_full: 'Repository' }[item.field] ||
                item.field}
              : {item.message}
            </p>
          ))
        : scan.reasoning?.error && <p role="alert">{scan.reasoning.error}</p>}
      {['pending', 'queued', 'running'].includes(scan.status) && (
        <Spinner label={review ? 'Review in progress' : 'Waiting for review results'} />
      )}
      {review && <SourceReviewFindings review={review} />}
    </div>
  );
}

export function SourceReviewFindings({ review }) {
  const hasBatches = Number.isInteger(review.batches_total) && review.batches_total > 0;
  const incomplete = hasBatches && review.batches_completed < review.batches_total;
  return (
    <>
      {hasBatches && (
        <div role="status" aria-live="polite">
          <p>
            Reviewed {review.batches_completed} of {review.batches_total} batches.
          </p>
          {incomplete && (
            <p>Partial results. Retry continues from saved batches when the inputs and model settings match.</p>
          )}
        </div>
      )}
      {review.no_changes && <p>The two commits have identical trees. No model request was needed.</p>}
      {review.unreviewed?.length > 0 && (
        <div role="note">
          <p>Changes not reviewed:</p>
          <ul>
            {review.unreviewed.map((item, index) => (
              <li key={index}>
                <code>{item.path}</code>: {item.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {!review.no_changes && !review.findings?.length && (
        <p>
          {review.files?.length === 0
            ? 'No source files could be reviewed. No model request was made.'
            : incomplete
              ? 'No findings in completed batches so far.'
              : 'No findings in the reviewed source. The changes may still contain defects.'}
        </p>
      )}
      {review.findings?.map((finding, index) => (
        <article
          key={index}
          style={{ border: '1px solid var(--border)', borderRadius: 10, padding: 18, marginBottom: 16 }}
        >
          <h2 style={{ fontSize: 18 }}>{finding.summary}</h2>
          <p className="mono">
            {finding.side} · {finding.path}:{finding.line} · {finding.confidence} confidence
          </p>
          <Markdown source={finding.explanation} />
          <h3 style={{ fontSize: 14 }}>Suggested correction</h3>
          <Markdown source={finding.remediation} />
        </article>
      ))}
    </>
  );
}
