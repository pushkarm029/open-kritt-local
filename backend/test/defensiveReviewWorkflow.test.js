import assert from 'node:assert/strict';
import { test } from 'node:test';

import {
  DEFENSIVE_REVIEW_STEPS,
  DEFENSIVE_REVIEW_WORKFLOW_NAME,
  ensureDefensiveReviewWorkflow,
} from '../src/lib/defensiveReviewWorkflow.js';
import { STEP_RESULTS_TABLE } from '../src/lib/constants.js';
import { createCommitDiffScan } from '../src/routes/scans.js';

function fixtureDatabase() {
  const steps = new Map();
  const scans = [];
  let workflow = null;
  const tx = {
    $executeRaw: async () => {},
    workflow: {
      findFirst: async () => workflow,
      create: async ({ data }) => {
        workflow = { id: 17n, ...data };
        return workflow;
      },
    },
    step: {
      create: async ({ data }) => {
        const step = { id: BigInt(steps.size + 1), boundSourceStepId: null, ...data };
        steps.set(step.id, step);
        return step;
      },
      findMany: async ({ where }) => where.id.in.map((id) => steps.get(id)).filter(Boolean),
    },
    scan: {
      count: async () => 0,
      create: async ({ data }) => {
        const scan = { id: BigInt(scans.length + 1), ...data };
        scans.push(scan);
        return scan;
      },
    },
  };
  return { db: { ...tx, $transaction: async (operation) => operation(tx) }, steps, scans, getWorkflow: () => workflow };
}

test('reserved workflow installs once with six inert source-review stages', async () => {
  const fixture = fixtureDatabase();
  const first = await ensureDefensiveReviewWorkflow(fixture.db);
  const again = await ensureDefensiveReviewWorkflow(fixture.db);
  assert.equal(first.id, again.id);
  assert.equal(first.name, DEFENSIVE_REVIEW_WORKFLOW_NAME);
  assert.equal(fixture.steps.size, 6);
  assert.deepEqual(
    first.stepIds.map((id) => fixture.steps.get(id).name),
    DEFENSIVE_REVIEW_STEPS.map((step) => step.name)
  );
  for (const step of fixture.steps.values()) {
    assert.equal(step.outputTable, STEP_RESULTS_TABLE);
    assert.equal(step.isLastStep, false);
    assert.deepEqual(JSON.parse(step.outputFormat), { unit_id: 'number', context: 'object', findings: 'array' });
  }
  fixture.steps.get(first.stepIds[0]).content = 'changed';
  await assert.rejects(
    () => ensureDefensiveReviewWorkflow(fixture.db),
    /reserved defensive review workflow has changed/
  );
  assert.equal(fixture.steps.size, 6);
});

test('commit scan stores the real workflow id only for workflow reviews', async () => {
  const fixture = fixtureDatabase();
  const common = {
    comparison_mode: 'commits',
    repo_kind: 'local',
    repo_full: 'demo',
    base_commit_sha: 'a'.repeat(40),
    commit_sha: 'b'.repeat(40),
  };
  const options = {
    db: fixture.db,
    localNames: new Set(['demo']),
    readConfig: async () => ({ baseUrl: 'http://localhost:9000/v1', model: 'fixture-model' }),
    providerConfigured: () => true,
  };
  const workflow = await createCommitDiffScan({ ...common, review_kind: 'workflow' }, options);
  assert.equal(workflow.workflowId, 17n);
  assert.equal(workflow.configuration.review_kind, 'workflow');
  assert.equal(workflow.configuration.source_review_version, 1);

  const quick = await createCommitDiffScan(common, options);
  assert.equal(quick.workflowId, 0n);
  assert.equal(quick.configuration.review_kind, 'quick');
  assert.equal(quick.configuration.source_review_version, undefined);
  assert.equal(fixture.steps.size, 6);
});
