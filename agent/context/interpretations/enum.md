

# Enumeration Output Interpretation

**Purpose:** Help @architect understand enum output and decide next actions

**Two Perspectives:**
1. **Findings:** What useful information was discovered
2. **Next Actions:** What commands should run based on this output

---

## [[interp-nxc-anon]] nxc Anonymous SMB Output

### Useful Findings:
- **Login successful:** Anonymous access allowed
- **Shares listed:** Accessible share names
- **Users enumerated:** If user listing worked
- **Access denied:** Anonymous blocked

### Next Actions:

IF login successful: → Run enum4linux-ng for detailed enumeration → Check shares for sensitive files → Document as "Anonymous Access" finding

IF access denied: → Try -u 'a' -p '' (guest account) → Move to authenticated methods if credentials obtained


---

## [[interp-nxc-guest]] nxc Guest SMB Output

### Useful Findings:
- **Guest login successful:** Guest account enabled
- **Same as anonymous:** Check shares and users

### Next Actions:

IF guest login successful: → Same as anonymous access → Document as "Guest Account Enabled" finding

IF access denied: → Anonymous/guest both blocked → Need credentials for further SMB enumeration



---

## [[interp-enum4linux]] enum4linux-ng Output

### Useful Findings:
- **Users listed:** Domain usernames
- **Shares listed:** Accessible shares with permissions
- **Policies:** Password policy, lockout settings
- **OS info:** Domain controller OS version

### Next Actions:


IF users found: → Save to userlist for bruteforce → Proceed to user enumeration commands

IF shares found: → Document accessible shares → Check for sensitive data in reports

IF password policy found: → Note minimum length, lockout threshold → Adjust bruteforce strategy accordingly



---

## [[interp-smbclient]] smbclient Output

### Useful Findings:
- **Share list:** All accessible shares
- **IPC$:** IPC share (normal)
- **NETLOGON/SYSVOL:** Domain shares (important)
- **Custom shares:** May contain sensitive data

### Next Actions:

IF NETLOGON/SYSVOL accessible: → Download contents (group policy, scripts) → Check for credentials in scripts

IF custom shares accessible: → Document for report → Consider downloading for analysis


---

## [[interp-ldap-nmap]] Nmap LDAP Scripts Output

### Useful Findings:
- **Domain name:** From LDAP root DSE
- **DC name:** Server name
- **Supported controls:** LDAP capabilities
- **Anonymous bind:** If allowed

### Next Actions:

IF anonymous bind allowed: → Run ldapsearch for full enumeration → Extract all users, groups, computers

IF anonymous bind denied: → Need credentials for LDAP enumeration → Try other enumeration methods first



---

## [[interp-ldapsearch]] ldapsearch Output

### Useful Findings:
- **Users:** All user objects with attributes
- **Groups:** Group memberships
- **Computers:** Joined computers
- **Password policy:** PWD properties

### Next Actions:

IF users extracted: → Build userlist for bruteforce → Look for interesting accounts (admin, svc, sql)

IF groups extracted: → Identify privileged groups (Domain Admins, etc.) → Note members for later targeting



---

## [[interp-nxc-users]] nxc --users Output

### Useful Findings:
- **Username list:** All domain users
- **Enabled/disabled:** Account status
- **Admin count:** Privileged accounts flagged

### Next Actions:

IF users found: → Save to userlist.txt → Prioritize admin/service accounts → Proceed to bruteforce phase (if in scope)

IF no users: → Try RID brute method → May need credentials for user enumeration



---

## [[interp-rid-brute]] RID Brute Output

### Useful Findings:
- **Users by RID:** Discovered accounts
- **Common RIDs:** 500 (Administrator), 501 (Guest), 1000+ (normal users)

### Next Actions:


IF users found: → Save to userlist.txt → Note Administrator RID 500 status

IF limited results: → May hit RID filter on DC → Try authenticated RID brute with credentials


---

## [[interp-net-rpc]] net rpc Output

### Useful Findings:
- **Group members:** All Domain Users group members
- **Complete list:** Often more complete than other methods

### Next Actions:

IF members listed: → Save to userlist.txt → Cross-reference with other enumeration results

IF access denied: → Need credentials for RPC enumeration