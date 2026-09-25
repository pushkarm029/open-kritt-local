import { prisma } from '../db.js';
import { STEP_RESULTS_TABLE } from './constants.js';

export const DEFENSIVE_REVIEW_WORKFLOW_NAME = 'Local defensive review v1';

const OUTPUT_FORMAT = JSON.stringify({ unit_id: 'number', context: 'object', findings: 'array' });

export const DEFENSIVE_REVIEW_STEPS = Object.freeze([
  { name: 'Scope', depth: 0, multiOutput: true, consumesAll: false, content: 'Identify changed source units.' },
  {
    name: 'Contract context',
    depth: 1,
    multiOutput: true,
    consumesAll: true,
    content: 'Record affected contracts and trust boundaries from the changed source.',
  },
  {
    name: 'Access and validation review',
    depth: 2,
    multiOutput: false,
    consumesAll: false,
    content: 'Review changed access checks and input validation for source-supported defects.',
  },
  {
    name: 'State and accounting review',
    depth: 2,
    multiOutput: false,
    consumesAll: false,
    content: 'Review changed state transitions and accounting for source-supported defects.',
  },
  {
    name: 'Check findings',
    depth: 3,
    multiOutput: false,
    consumesAll: false,
    content: 'Check each finding against changed source lines and identify a correction.',
  },
  {
    name: 'Report',
    depth: 4,
    multiOutput: false,
    consumesAll: true,
    content: 'Summarize checked findings, review coverage, and limitations.',
  },
]);

export function isDefensiveReviewWorkflowName(name) {
  return name === DEFENSIVE_REVIEW_WORKFLOW_NAME;
}

function matchesBlueprint(workflow, steps) {
  if (workflow.stepIds?.length !== DEFENSIVE_REVIEW_STEPS.length || steps.length !== DEFENSIVE_REVIEW_STEPS.length) {
    return false;
  }
  const byId = new Map(steps.map((step) => [step.id.toString(), step]));
  return workflow.stepIds.every((id, index) => {
    const step = byId.get(id.toString());
    const expected = DEFENSIVE_REVIEW_STEPS[index];
    return (
      step?.name === expected.name &&
      step.content === expected.content &&
      step.depth === expected.depth &&
      step.multiOutput === expected.multiOutput &&
      step.consumesAll === expected.consumesAll &&
      step.isLastStep === false &&
      step.outputTable === STEP_RESULTS_TABLE &&
      step.outputFormat === OUTPUT_FORMAT &&
      step.boundSourceStepId == null
    );
  });
}

export async function ensureDefensiveReviewWorkflow(client = prisma) {
  const install = async (tx) => {
    await tx.$executeRaw`SELECT pg_advisory_xact_lock(hashtext('open-kritt-defensive-review-v1'))`;
    const existing = await tx.workflow.findFirst({
      where: { name: DEFENSIVE_REVIEW_WORKFLOW_NAME },
      orderBy: { insertedAt: 'asc' },
    });
    if (existing) {
      const steps = await tx.step.findMany({ where: { id: { in: existing.stepIds || [] } } });
      if (!matchesBlueprint(existing, steps)) {
        throw new Error('The reserved defensive review workflow has changed. Restore it before starting a review.');
      }
      return existing;
    }

    const stepIds = [];
    for (const step of DEFENSIVE_REVIEW_STEPS) {
      const created = await tx.step.create({
        data: {
          ...step,
          outputFormat: OUTPUT_FORMAT,
          outputTable: STEP_RESULTS_TABLE,
          isLastStep: false,
        },
      });
      stepIds.push(created.id);
    }
    return tx.workflow.create({
      data: {
        name: DEFENSIVE_REVIEW_WORKFLOW_NAME,
        description: 'Read-only source review of two committed revisions.',
        stepIds,
        extra: [],
      },
    });
  };
  return typeof client.$transaction === 'function' ? client.$transaction(install) : install(client);
}
