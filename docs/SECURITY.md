# Security notes

Roles are `admin`, `engineer`, `approver`, and `observer`. Promotion and rollback require approver or admin. Policy changes require admin. The browser never receives a kubeconfig. Pod log reads go through the API and are redacted for token-like assignments. There is no pod exec endpoint.

The serving process loads one JSON artifact at startup. Training writes coefficients, means, and scales. It does not register a pickle as the inference artifact.

Demo passwords, the Compose database password, the Grafana admin password, and `KUBEML_JWT_SECRET=local-dev-only-change-me` are local only. Replace them before any shared deployment. The scheduler token in the Helm chart is a service credential for the retraining CronJob. Rotate it by changing the Secret and restarting the platform.

Inference logs keep request ids, model version, latency, and validation status. Raw feature values are stored only for the synthetic traffic generator, marked `synthetic_authorised`.

Feature drift cannot be turned into an automatic rollback from the policy API.
