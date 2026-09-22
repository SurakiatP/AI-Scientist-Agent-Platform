package scilab.approval

default allow := false
default requires_approval := true

side_effects := {
    "publish",
    "external_write",
    "delete",
    "credential_use",
    "network_change",
}

allow if {
    input.effect == "read"
}

requires_approval := false if allow

policy_rule := "read-only" if allow
policy_rule := "human-review" if not allow

reason := "read-only action" if allow
reason := "side effect or unknown action" if not allow

decision := {
    "allow": allow,
    "requires_approval": requires_approval,
    "policy_rule": policy_rule,
    "reason": reason,
}
