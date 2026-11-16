# Offline AWS CLI validation

`ozagia.aws_cli.validate_aws_command(argv)` is an offline hint for
`aws <service> <operation>` names. Pass an argv sequence, not a shell command
string. The validator does not replace, intercept, or run the command.

When botocore is installed, it reads local `service-2` model files through
botocore's loader. It does not create a botocore session, load credentials,
choose a profile, or make a network request. If the optional model dependency
is unavailable, the result says `available=False` and
`reason="botocore_unavailable"`. Tests can inject a botocore-shaped model
mapping without requiring botocore or network access.

Validation is deliberately limited to service and operation names plus simple
string enum values on modeled `--option value` arguments. It does not check
required parameters, arbitrary flags, resource identifiers, structured
JSON/YAML values, IAM permissions, or whether a command is safe to execute.
Unknown flags are left alone. Results contain no argument values.

This is an offline parser/model lookup only, not an emulator pre-flight or a
live AWS check. Any later live use must remain explicitly approved, read-only,
bound to a named environment and region, and must not log secrets.
