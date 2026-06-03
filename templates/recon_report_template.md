# Reconnaissance Phase Report

**Assessment Date:** <date>  
**Target Network:** <target>  
**Scope:** <scope>  
**Classification:** Confidential

---

## Executive Summary

<High-level summary of findings: total hosts discovered, key findings, critical/high severity issues>

### Key Findings Summary

| Metric | Value |
|--------|-------|
| Total Hosts Discovered | <count> |
| Live SMB Hosts | <count> |
| Domain Controllers | <count> |
| Active Directory Domains | <count> |
| <Additional metrics> | <value> |

### Critical Observations

1. <Key finding 1>
2. <Key finding 2>
3. <Key finding 3>

---

## Scope of Assessment

| Item | Details |
|------|---------|
| Target Network | <target range> |
| Assessment Date | <date> |
| Tools Used | <tools> |
| Methodology | <methods> |
| Assessor Position | <attacker IP> |

### Tools Employed

| Tool | Purpose |
|------|---------|
| <tool> | <purpose> |

---

## Active Directory Infrastructure

### Domain Controllers Identified

| Hostname | IP Address | Domain | OS | Role |
|----------|------------|--------|-----|------|
| <hostname> | <IP> | <domain> | <OS> | <role> |

### Domain Controller Services

#### <Hostname> (<IP>)

| Service | Port | Status | Notes |
|---------|------|--------|-------|
| <service> | <port> | <status> | <notes> |

### Trust Relationships

| Trust Direction | From Domain | To Domain | Trust Type | Notes |
|----------------|-------------|-----------|------------|-------|
| <direction> | <from> | <to> | <type> | <notes> |

---

## Host Inventory

### Complete Discovery Results

| IP Address | Hostname | OS/Type | Domain | Key Services | Risk Level |
|------------|----------|---------|--------|--------------|------------|
| <IP> | <hostname> | <OS> | <domain> | <services> | <level> |

### Detailed Host Analysis

#### <Hostname> (<IP>)

| Attribute | Value |
|-----------|-------|
| Role | <role> |
| OS | <OS> |
| Domain | <domain> |
| Services | <services> |

---

## Security Findings

### CRITICAL Severity

#### C-1: <Finding Title>

| Attribute | Value |
|-----------|-------|
| Finding ID | C-1 |
| Affected Hosts | <count> |
| Affected IPs | <IPs> |
| CVSS Score | <score> |
| MITRE Technique | <technique> |

**Description:** <description>

**Evidence:**
```
<evidence>
```

**Business Impact:** <impact>

**Recommendation:** <recommendation>

---

### HIGH Severity

#### H-1: <Finding Title>

(Same structure as Critical)

---

### MEDIUM Severity

#### M-1: <Finding Title>

(Same structure as Critical)

---

### Informational

#### I-1: <Finding Title>

(Same structure as Critical, without CVSS)

---

## Risk Matrix

| Finding ID | Vulnerability | CVSS | Affected Hosts | Risk Level |
|------------|--------------|------|----------------|------------|
| C-1 | <vuln> | <score> | <count> | CRITICAL |
| H-1 | <vuln> | <score> | <count> | HIGH |
| M-1 | <vuln> | <score> | <count> | MEDIUM |
| I-1 | <vuln> | N/A | <count> | INFO |

---

## Remediation Recommendations

### Immediate Actions (0-30 Days)

1. **<Finding ID>: <Title>**
   - <recommendation>

### Short-Term Actions (30-90 Days)

2. **<Finding ID>: <Title>**
   - <recommendation>

### Long-Term Actions (90+ Days)

3. **<Finding ID>: <Title>**
   - <recommendation>

---

## Next Steps for Enumeration Phase

Based on reconnaissance findings, the following enumeration activities are recommended:

### Priority 1: <Priority>

| Action | Target | Tool/Notes |
|--------|--------|------------|
| <action> | <target> | <tool/notes> |

### Priority 2: <Priority>

| Action | Target | Tool/Notes |
|--------|--------|------------|
| <action> | <target> | <tool/notes> |

### Priority 3: <Priority>

| Action | Target | Tool/Notes |
|--------|--------|------------|
| <action> | <target> | <tool/notes> |

---

## Conclusion

<Summary of assessment: what was discovered, security posture overview, recommendations for next phase>

---

## Appendix A: Evidence Files Reference

| Finding File | Description |
|-------------|-------------|
| <file> | <description> |

---

*Report Generated: <date>*  
*Assessment Type: Reconnaissance Phase*  
*Report Version: 1.0*
