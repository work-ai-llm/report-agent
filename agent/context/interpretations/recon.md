

# Reconnaissance Output Interpretation

**Purpose:** Help @architect understand recon output and decide next actions

**Two Perspectives:**
1. **Findings:** What useful information was discovered
2. **Next Actions:** What commands should run based on this output

---

## [[interp-nxc-smb]] nxc smb Output

### Useful Findings:
- **Live hosts:** Any IP that responds has SMB enabled
- **OS version:** Windows version hints at patch level
- **Domain name:** Often revealed in SMB banner
- **Signing required:** If "SMB signing: required", relay attacks won't work

### Next Actions (Decision Logic):

IF hosts found: → Proceed to "Find DC IP" commands → Record all IPs in findings/hosts.txt

IF domain name revealed: → Use domain for nslookup SRV queries → Update scope documentation

IF no hosts found: → Expand IP range → Try nmap -sP ping scan first



---

## [[interp-nmap-ping]] nmap Ping Scan Output

### Useful Findings:
- **Live hosts:** Hosts that respond to ping
- **Hostname:** Often reveals purpose (DC01, SQL01, etc.)
- **Filtered hosts:** May indicate firewall, still worth scanning

### Next Actions:

IF live hosts found: → Run nmap -sV --top-ports 50 on each → Prioritize hosts with interesting hostnames

IF no hosts respond: → ICMP may be blocked → Try nmap -Pn (skip ping) with port scan directly


---

## [[interp-nmap-sv]] nmap Service Version Output

### Useful Findings:
- **Open ports:** 445 (SMB), 389 (LDAP), 88 (Kerberos), 53 (DNS)
- **Service versions:** Old versions may have known exploits
- **OS detection:** Windows Server version hints at AD functional level

### Next Actions:

IF port 445 open: → Run nxc smb anonymous scans → Run enum4linux-ng

IF port 389 open: → Run ldapsearch commands → This is likely a DC

IF port 88 open: → This is a Kerberos server (DC) → Record as DC IP for later enumeration

IF port 53 open: → Try dig axfr zone transfer



---

## [[interp-smb-vuln]] SMB Vulnerability Scan Output

### Useful Findings:
- **VULNERABLE:** Exploitable (EternalBlue, etc.)
- **NOT VULNERABLE:** Patched, try other vectors
- **Unknown:** May need manual verification

### Next Actions:

IF VULNERABLE: → Document severity (Critical) → Consider exploitation (if in scope) → May skip enumeration, go straight to exploit

IF NOT VULNERABLE: → Continue with enumeration phase → Focus on credential attacks instead


---

## [[interp-nmcli]] nmcli Output

### Useful Findings:
- **DNS servers:** Usually includes DC IP
- **Domain suffix:** Reveals AD domain name
- **Gateway:** Network boundaries

### Next Actions:

IF DNS server identified: → Use as <dns_server> in nslookup commands

IF domain suffix found: → Use as <domain> in all subsequent commands

IF multiple interfaces: → Note which interface is on target network → Use correct interface for responder/mitm6



---

## [[interp-nslookup-dc]] nslookup DC SRV Output

### Useful Findings:
- **DC hostname:** Full name of Domain Controller
- **DC IP:** Extracted from hostname resolution
- **Multiple DCs:** All listed SRV records

### Next Actions:

IF DC found: → Record IP as primary DC target → Proceed to Enumeration phase → Use DC IP for ldapsearch, nxc --users

IF multiple DCs: → Test all of them (some may have different configs) → Prioritize first listed (often PDC emulator)

IF no DC found: → Domain name may be wrong → Try nmap -p 88 to find Kerberos servers


---

## [[interp-kerberos]] Kerberos Port Scan Output

### Useful Findings:
- **Port 88 open:** Kerberos server (likely DC)
- **Hostname:** May reveal domain name

### Next Actions:


IF port 88 open: → Treat this IP as DC → Proceed to Enumeration phase → Use for ldapsearch, nxc --users

IF multiple IPs with port 88: → All are likely DCs → Enumerate all of them



---

## [[interp-axfr]] DNS Zone Transfer Output

### Useful Findings:
- **All DNS records:** Complete network map
- **Hostnames:** Reveals server purposes
- **Subdomains:** May reveal additional domains

### Next Actions:


IF zone transfer successful: → Extract all IPs and hostnames → Add all to target list → Prioritize DC, DNS, SQL servers

IF zone transfer failed: → Not misconfigured (common) → Continue with other recon methods