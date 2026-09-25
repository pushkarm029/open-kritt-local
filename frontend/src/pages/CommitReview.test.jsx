import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { CommitFields, CommitReviewResults, SourceReviewFindings, commitReviewDraft } from './CommitReview.jsx';

describe('two-commit review', () => {
  it('copies only repository and pinned revisions when duplicating', () => {
    expect(
      commitReviewDraft({
        repoKind: 'local',
        repoFull: 'demo',
        baseCommitSha: 'a'.repeat(40),
        commitSha: 'b'.repeat(40),
        diffReview: { findings: ['old'] },
        id: '8',
      })
    ).toEqual({
      repo_kind: 'local',
      repo_full: 'demo',
      base_commit_sha: 'a'.repeat(40),
      commit_sha: 'b'.repeat(40),
      comparison_mode: 'commits',
    });
  });

  it('requires two explicit commit IDs and labels the inputs', () => {
    const html = renderToStaticMarkup(createElement(CommitFields, { draft: commitReviewDraft(), onChange: () => {} }));
    expect(html).toContain('Base commit');
    expect(html).toContain('Head commit');
    expect(html.match(/required=""/g)).toHaveLength(2);
    expect(html).not.toContain('placeholder="HEAD"');
  });

  it('renders base-side evidence, skipped changes, and safe source text', () => {
    const html = renderToStaticMarkup(
      createElement(SourceReviewFindings, {
        review: {
          unreviewed: [{ path: 'asset.bin', reason: 'Binary file' }],
          findings: [
            {
              summary: '<script>alert(1)</script>',
              path: 'removed.py',
              side: 'base',
              line: 7,
              confidence: 'medium',
              explanation: 'Source evidence',
              remediation: 'Restore the check',
            },
          ],
        },
      })
    );
    expect(html).toContain('base');
    expect(html).toContain('removed.py');
    expect(html).toContain('asset.bin');
    expect(html).not.toContain('<script>');
  });

  it('retains export and delete for a completed review', () => {
    const html = renderToStaticMarkup(
      createElement(
        MemoryRouter,
        {},
        createElement(CommitReviewResults, {
          scan: {
            id: '1',
            repoFull: 'demo',
            status: 'completed',
            model: 'fixture',
            baseCommitSha: 'a'.repeat(40),
            commitSha: 'b'.repeat(40),
            diffReview: { no_changes: true, findings: [] },
          },
          reload: () => {},
        })
      )
    );
    expect(html).toContain('Export review');
    expect(html).toContain('Delete');
    expect(html).toContain('identical trees');
  });
});
