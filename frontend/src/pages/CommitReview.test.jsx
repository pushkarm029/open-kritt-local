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
      review_kind: 'quick',
    });
    expect(commitReviewDraft().review_kind).toBe('workflow');
    expect(commitReviewDraft({ configuration: { review_kind: 'workflow' } }).review_kind).toBe('workflow');
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

  it('distinguishes skipped source from a review with no findings', () => {
    const skipped = renderToStaticMarkup(
      createElement(SourceReviewFindings, {
        review: {
          no_changes: false,
          files: [],
          findings: [],
          unreviewed: [{ path: 'asset.bin', reason: 'Binary file' }],
        },
      })
    );
    expect(skipped).toContain('No source files could be reviewed. No model request was made.');
    expect(skipped).toContain('asset.bin');
    expect(skipped).not.toContain('No findings in the reviewed source');

    const reviewed = renderToStaticMarkup(
      createElement(SourceReviewFindings, {
        review: { no_changes: false, files: [{ path: 'example.py' }], findings: [], unreviewed: [] },
      })
    );
    expect(reviewed).toContain('No findings in the reviewed source');
    expect(reviewed).not.toContain('No model request was made');
  });

  it('shows saved batch progress without claiming the whole review is finished', () => {
    const review = {
      files: [{ path: 'example.py' }],
      findings: [],
      batches_completed: 2,
      batches_total: 8,
    };
    const partial = renderToStaticMarkup(createElement(SourceReviewFindings, { review }));
    expect(partial).toContain('Reviewed 2 of 8 batches.');
    expect(partial).toContain('Partial results.');
    expect(partial).toContain('No findings in completed batches so far.');
    expect(partial).not.toContain('No findings in the reviewed source.');

    const completed = renderToStaticMarkup(
      createElement(SourceReviewFindings, { review: { ...review, batches_completed: 8 } })
    );
    expect(completed).toContain('Reviewed 8 of 8 batches.');
    expect(completed).not.toContain('Partial results.');
    expect(completed).toContain('No findings in the reviewed source.');
  });

  it('does not claim a defensive workflow is clean before its report finishes', () => {
    const html = renderToStaticMarkup(
      createElement(SourceReviewFindings, {
        review: {
          files: [{ path: 'code.py' }],
          findings: [],
          workflow: { name: 'Local defensive review v1', stages: [{ name: 'Scope', status: 'completed' }] },
        },
      })
    );
    expect(html).toContain('Findings will appear after candidate checks and the report finish.');
    expect(html).not.toContain('No findings in the reviewed source.');
  });

  it('renders defensive stages and limits as read-only results', () => {
    const html = renderToStaticMarkup(
      createElement(SourceReviewFindings, {
        review: {
          files: [{ path: 'code.py' }],
          findings: [],
          workflow: {
            name: 'Local defensive review v1',
            version: 1,
            stages: [{ name: 'Scope', status: 'completed', completed: 3, total: 3 }],
            limitations: ['One file was skipped.'],
            report: {
              summary: 'One source candidate needs validation.',
              files_reviewed: 1,
              review_passes: 2,
              supported: 0,
              uncertain: 1,
              dismissed: 3,
            },
            uncertain_findings: [
              {
                summary: 'Possible unchecked path',
                side: 'head',
                path: 'code.py',
                line: 7,
                confidence: 'low',
                explanation: 'The changed branch may lack a guard.',
                remediation: 'Check the caller contract.',
                validation_explanation: 'The caller is outside the reviewed diff.',
              },
            ],
          },
        },
      })
    );
    expect(html).toContain('Local defensive review v1');
    expect(html).toContain('Scope: completed (3/3)');
    expect(html).toContain('One file was skipped.');
    expect(html).toContain('One source candidate needs validation.');
    expect(html).toContain('Files reviewed: 1');
    expect(html).toContain('Needs validation (1)');
    expect(html).toContain('The caller is outside the reviewed diff.');
    expect(html).not.toContain('No findings in the reviewed source.');
    expect(html).not.toContain('malicious_input_example');
  });
});
