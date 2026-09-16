# Access add-on: read-only EKS + read-only production DB (box stays outside the VPC)

Threat model: the agent box runs a coding agent with permissions bypassed on
input that arrives from chat. Treat it as semi-trusted. It gets **no network
route into any VPC**; its only privileged contact is the public EKS API,
authenticated by an instance role. Everything it may do is bounded by that
role: cluster-wide read (no Secrets, no writes) plus exec into one tiny
`psql` pod that holds a read-only database login.

## AWS side (user-run, admin creds)

```sh
aws iam create-role --role-name <agent>-agent --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
aws iam put-role-policy --role-name <agent>-agent --policy-name eks-describe --policy-document file://policy.json   # eks:DescribeCluster on the one cluster ARN, nothing else
aws iam create-instance-profile --instance-profile-name <agent>-agent
aws iam add-role-to-instance-profile --instance-profile-name <agent>-agent --role-name <agent>-agent
aws ec2 associate-iam-instance-profile --instance-id <id> --iam-instance-profile Name=<agent>-agent
aws ec2 modify-instance-metadata-options --instance-id <id> --http-tokens required --http-put-response-hop-limit 1   # IMDSv2 only
aws eks create-access-entry --cluster-name <cluster> --principal-arn arn:aws:iam::<acct>:role/<agent>-agent --kubernetes-groups <agent>-agent --type STANDARD
aws eks associate-access-policy --cluster-name <cluster> --principal-arn <role-arn> --policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy --access-scope type=cluster
```

Traps: write the policy JSON to a file with a quoted heredoc (an unquoted one
mangled `:cluster` into `luster` once); pass the principal ARN as a quoted
literal to the `eks` calls. `AmazonEKSViewPolicy` = the `view` ClusterRole:
namespaced reads, **no Secrets**, and no cluster-scoped objects — add a small
ClusterRole for nodes/namespaces/metrics bound to the access entry's group.

## Kubernetes side (user-run, cluster-admin kubectl)

Namespace `<agent>`: a `psql` Deployment (`postgres:17-alpine`, `sleep
infinity`, non-root, no service-account token) with `PG*` env from a Secret
in that namespace, plus a Role granting only `pods/exec create`, bound to the
access entry's group. Manifests: `omni-k8s.yaml` in this directory is the
worked example.

Create the DB role with a **Job in the app's namespace** that gets
`DATABASE_URL` from the app's existing Secret — the master password never
leaves the cluster. Put the SQL in a ConfigMap, not in `args`: Kubernetes
rewrites `$$` in `command`/`args` to `$`, which breaks `DO $$ … $$`. On RDS
the master isn't a superuser, so don't `ALTER ROLE … NOSUPERUSER`.

```sql
SELECT 'CREATE ROLE <ro> LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='<ro>') \gexec
ALTER ROLE <ro> PASSWORD :'pw';
ALTER ROLE <ro> CONNECTION LIMIT 5;
GRANT pg_read_all_data TO <ro>;
ALTER ROLE <ro> SET default_transaction_read_only = on;
ALTER ROLE <ro> SET statement_timeout = '60s';
ALTER ROLE <ro> SET idle_in_transaction_session_timeout = '60s';
```

Point `PGHOST` at the Aurora **reader** endpoint so queries never touch the
writer. Generate the password locally (`openssl rand`), feed it to both
Secrets in one shell, `unset` it; delete the bootstrap Secret/ConfigMap/Job
afterwards.

## Box side

Install AWS CLI v2; `aws eks update-kubeconfig` with the instance role (no
static creds); set `KUBECONFIG`/`AWS_REGION` in the base env; add a wrapper:

```sh
exec kubectl exec -i -n <agent> deploy/psql -- psql --set=ON_ERROR_STOP=1 "$@"
```

Tell the agent about it in `$CLAUDE_CONFIG_DIR/CLAUDE.md` (read-only,
production data, aggregate/redact, reader lag).

## Verify (all from the box)

- `aws sts get-caller-identity` shows the instance role.
- `kubectl get pods -A` works; `kubectl get secrets -n <app>` and
  `kubectl delete pod x -n <app>` are **Forbidden**.
- `prod-psql -Atc "select current_user, pg_is_in_recovery()"` → `<ro>|t`;
  `create table` fails with "read-only transaction".

## Blast radius if the box is compromised

Read-only cluster view (pod logs included — they may contain PII), and
read-only DB queries under the role's limits. No VPC reach, no Secrets, no
writes, no node access. Rotate by re-running the Job with a new password and
`kubectl rollout restart deploy/psql`.
