export type OpenPr = {
  repo: string;
  ref: string;
  title: string;
  url: string;
  opened: string;
  age_days: number;
  ci: 'green' | 'red' | 'pending' | 'none';
  draft: boolean;
  unresolved_comments: number;
  plane_url: string;
};
export type DeployRow = {
  repo: string;
  ref: string;
  title: string;
  pr_url: string;
  merged_at: string;
  action: string;
};
export type LastRun = {
  started: string;
  ended: string;
  sessions: number;
  prs_opened: number;
  feedback_handled: number;
  stop_reason: string;
  telemetry: string;
};
export type Pace = {
  five_hour_used: number;
  five_hour_resets: number;
  seven_day_used: number;
  seven_day_resets: number;
  elapsed_pct: number;
};
export type Snapshot = {
  schema: number;
  missing?: boolean;
  generated_at?: string;
  paused?: boolean;
  last_run?: LastRun | null;
  pace?: Pace | null;
  open_prs?: OpenPr[];
  deploy_pending?: DeployRow[];
  declined?: { ref: string; reason: string; at: string }[];
};
