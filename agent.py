"""
Pre-Call Intelligence Agent
Two-stage pipeline: Perplexity (research) → Claude (synthesis)
Supports optional Nutanix Collector CSV for environment-aware briefs.
All file data is processed in memory and never written to disk.
"""

import json
import io
import csv
import requests
import anthropic
from dataclasses import dataclass, field
from typing import Optional

try:
    import pandas as pd
    _PANDAS_AVAILABLE = True
except ImportError:
    _PANDAS_AVAILABLE = False


@dataclass
class ProspectInput:
    company_name: str
    contact_name: str
    contact_title: str
    segment: str  # "commercial" or "smb"
    our_product: str
    notes: Optional[str] = None


@dataclass
class NutanixEnvironment:
    """Parsed environment summary — from Nutanix Collector CSV or RVTools XLSX. In-memory only."""
    raw_rows: int = 0
    clusters: list[str] = field(default_factory=list)
    total_nodes: int = 0
    total_hosts: int = 0
    total_vcpus: int = 0
    total_ram_gb: float = 0.0
    total_storage_tb: float = 0.0
    hypervisors: list[str] = field(default_factory=list)
    aos_versions: list[str] = field(default_factory=list)
    workload_types: list[str] = field(default_factory=list)
    vm_count: int = 0
    powered_on_vms: int = 0
    os_types: list[str] = field(default_factory=list)
    host_models: list[str] = field(default_factory=list)
    snapshots: int = 0
    health_warnings: int = 0
    vcenter_version: str = ""
    hypervisor_versions: list[str] = field(default_factory=list)
    vcpu_overcommit: float = 0.0
    vram_overcommit: float = 0.0
    source_type: str = "nutanix_csv"
    summary_text: str = ""

    def to_prompt_block(self) -> str:
        if not self.summary_text:
            return ""
        if self.source_type == "rvtools":
            os_str = ", ".join(self.os_types[:6]) if self.os_types else "N/A"
            hw_str = ", ".join(self.host_models[:2]) if self.host_models else "N/A"
            overcommit = f"{self.vcpu_overcommit:.1f}x vCPU overcommit" if self.vcpu_overcommit else ""
            return f"""
VMWARE ENVIRONMENT DATA (from RVTools export):
- Clusters: {', '.join(self.clusters) if self.clusters else 'N/A'}
- Physical Hosts: {self.total_hosts} ({hw_str})
- Total VMs: {self.vm_count} ({self.powered_on_vms} powered on)
- vCPUs allocated: {self.total_vcpus}{f' — {overcommit}' if overcommit else ''}
- Physical RAM: {self.total_ram_gb:.0f} GB
- Total Datastore Capacity: {self.total_storage_tb:.1f} TB
- Hypervisor: {', '.join(self.hypervisors) if self.hypervisors else 'VMware vSphere'}
- vCenter Version: {self.vcenter_version if self.vcenter_version else 'N/A'}
- Guest OS mix: {os_str}
- Snapshots: {self.snapshots}
- Health warnings: {self.health_warnings}

This is a VMware environment. Focus on HCI consolidation, migration from vSphere, or Nutanix Cloud Platform as a replacement/complement. Reference the specific numbers above when building talking points.
"""
        return f"""
NUTANIX ENVIRONMENT DATA (from Collector CSV — {self.raw_rows} records):
- Clusters: {', '.join(self.clusters) if self.clusters else 'N/A'}
- Total Nodes: {self.total_nodes}
- Total VMs: {self.vm_count}
- Total vCPUs: {self.total_vcpus}
- Total RAM: {self.total_ram_gb:.0f} GB
- Total Storage: {self.total_storage_tb:.1f} TB
- Hypervisors in use: {', '.join(set(self.hypervisors)) if self.hypervisors else 'N/A'}
- AOS Versions: {', '.join(set(self.aos_versions)) if self.aos_versions else 'N/A'}
- Workload types detected: {', '.join(set(self.workload_types)) if self.workload_types else 'N/A'}

Additional notes: {self.summary_text}
"""


def parse_nutanix_csv(file_bytes: bytes) -> NutanixEnvironment:
    """
    Parse a Nutanix Collector CSV from raw bytes.
    Processed entirely in memory — file is never written to disk.
    Handles varied column naming across different Collector versions.
    """
    env = NutanixEnvironment()

    try:
        text = file_bytes.decode("utf-8-sig")  # handle BOM
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1")
    text = text.replace("\x00", "")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    env.raw_rows = len(rows)

    if not rows:
        env.summary_text = "CSV was empty or could not be parsed."
        return env

    def get_col(row: dict, *candidates: str) -> str:
        lower_row = {k.lower().strip(): v for k, v in row.items()}
        for c in candidates:
            val = lower_row.get(c.lower().strip(), "")
            if val and str(val).strip():
                return str(val).strip()
        return ""

    clusters_seen = set()
    hypervisors_seen = set()
    versions_seen = set()
    workloads_seen = set()
    total_nodes = 0
    total_vcpus = 0
    total_ram = 0.0
    total_storage = 0.0
    total_vms = 0

    for row in rows:
        cluster = get_col(row, "cluster name", "cluster", "cluster_name", "name")
        if cluster:
            clusters_seen.add(cluster)

        nodes_val = get_col(row, "number of nodes", "node count", "nodes", "num_nodes", "hosts")
        try:
            total_nodes += int(float(nodes_val))
        except (ValueError, TypeError):
            pass

        vcpu_val = get_col(row, "total vcpus", "vcpus", "num vcpus", "cpu count", "total_vcpus")
        try:
            total_vcpus += int(float(vcpu_val))
        except (ValueError, TypeError):
            pass

        ram_val = get_col(row, "total memory gb", "memory gb", "ram gb", "total ram", "memory (gb)", "total_memory_gb")
        try:
            total_ram += float(ram_val)
        except (ValueError, TypeError):
            pass

        stor_val = get_col(row, "total storage tb", "storage tb", "capacity tb", "raw storage tb", "total_storage_tb", "storage (tb)")
        try:
            total_storage += float(stor_val)
        except (ValueError, TypeError):
            stor_gb = get_col(row, "total storage gb", "storage gb", "capacity gb")
            try:
                total_storage += float(stor_gb) / 1024
            except (ValueError, TypeError):
                pass

        vm_val = get_col(row, "total vms", "vm count", "num vms", "number of vms", "vms")
        try:
            total_vms += int(float(vm_val))
        except (ValueError, TypeError):
            pass

        hyp = get_col(row, "hypervisor", "hypervisor type", "hypervisor_type", "virtualization")
        if hyp:
            hypervisors_seen.add(hyp)

        ver = get_col(row, "aos version", "nos version", "nutanix version", "version", "aos_version")
        if ver:
            versions_seen.add(ver)

        wl = get_col(row, "workload type", "workload", "workloads", "use case", "workload_type")
        if wl:
            for w in wl.split(","):
                w = w.strip()
                if w:
                    workloads_seen.add(w)

    env.clusters = sorted(clusters_seen)
    env.total_nodes = total_nodes
    env.total_vcpus = total_vcpus
    env.total_ram_gb = total_ram
    env.total_storage_tb = total_storage
    env.vm_count = total_vms
    env.hypervisors = list(hypervisors_seen)
    env.aos_versions = list(versions_seen)
    env.workload_types = list(workloads_seen)

    parts = []
    if env.clusters:
        parts.append(f"{len(env.clusters)} cluster(s): {', '.join(env.clusters[:5])}")
    if total_nodes:
        parts.append(f"{total_nodes} nodes")
    if total_vms:
        parts.append(f"{total_vms} VMs")
    if total_ram:
        parts.append(f"{total_ram:.0f} GB RAM")
    if total_storage:
        parts.append(f"{total_storage:.1f} TB storage")
    if hypervisors_seen:
        parts.append(f"hypervisor: {', '.join(hypervisors_seen)}")
    if versions_seen:
        parts.append(f"AOS: {', '.join(versions_seen)}")

    env.summary_text = "; ".join(parts) if parts else "Collector data parsed but no standard fields detected."
    return env


def parse_rvtools_xlsx(file_bytes: bytes) -> NutanixEnvironment:
    """
    Parse an RVTools Excel export from raw bytes.
    Processed entirely in memory — file is never written to disk.
    """
    if not _PANDAS_AVAILABLE:
        raise RuntimeError("pandas is required to parse RVTools files: pip install pandas openpyxl")

    env = NutanixEnvironment()
    env.source_type = "rvtools"

    xl = pd.ExcelFile(io.BytesIO(file_bytes))

    def read_sheet(name):
        if name in xl.sheet_names:
            return xl.parse(name)
        return pd.DataFrame()

    # --- vInfo: VM inventory ---
    vinfo = read_sheet("vInfo")
    if not vinfo.empty:
        env.vm_count = len(vinfo)
        env.raw_rows = env.vm_count
        if "Powerstate" in vinfo.columns:
            env.powered_on_vms = int((vinfo["Powerstate"] == "poweredOn").sum())
        os_col = "OS according to the VMware Tools"
        if os_col in vinfo.columns:
            powered = vinfo[vinfo.get("Powerstate", pd.Series()) == "poweredOn"] if "Powerstate" in vinfo.columns else vinfo
            env.os_types = [str(k) for k in powered[os_col].value_counts().index if pd.notna(k)][:8]
        if "CPUs" in vinfo.columns:
            env.total_vcpus = int(pd.to_numeric(vinfo["CPUs"], errors="coerce").sum())

    # --- vHost: physical hosts ---
    vhost = read_sheet("vHost")
    if not vhost.empty:
        env.total_hosts = len(vhost)
        env.total_nodes = env.total_hosts
        if "CPU Model" in vhost.columns:
            env.host_models = [str(m) for m in vhost["CPU Model"].dropna().unique()]
        if "# vCPUs" in vhost.columns:
            env.total_vcpus = int(pd.to_numeric(vhost["# vCPUs"], errors="coerce").sum())
        if "# Memory" in vhost.columns:
            phys_ram_mib = pd.to_numeric(vhost["# Memory"], errors="coerce").sum()
            env.total_ram_gb = phys_ram_mib / 1024
        if "vRAM" in vhost.columns and "# Memory" in vhost.columns:
            vram_mib = pd.to_numeric(vhost["vRAM"], errors="coerce").sum()
            phys_mib = pd.to_numeric(vhost["# Memory"], errors="coerce").sum()
            if phys_mib > 0:
                env.vram_overcommit = round(vram_mib / phys_mib, 2)
        if "# Cores" in vhost.columns and env.total_vcpus > 0:
            total_cores = pd.to_numeric(vhost["# Cores"], errors="coerce").sum()
            if total_cores > 0:
                env.vcpu_overcommit = round(env.total_vcpus / total_cores, 2)

    # --- vCluster: cluster names ---
    vcluster = read_sheet("vCluster")
    if not vcluster.empty and "Name" in vcluster.columns:
        # Filter out empty/placeholder clusters (NumHosts=0)
        active = vcluster
        if "NumHosts" in vcluster.columns:
            active = vcluster[pd.to_numeric(vcluster["NumHosts"], errors="coerce") > 0]
        env.clusters = [str(n) for n in active["Name"].dropna()] or [str(n) for n in vcluster["Name"].dropna()]

    # --- vDatastore: storage ---
    vds = read_sheet("vDatastore")
    if not vds.empty and "Capacity MiB" in vds.columns:
        total_mib = pd.to_numeric(vds["Capacity MiB"], errors="coerce").sum()
        env.total_storage_tb = total_mib / 1024 / 1024

    # --- vSource: vCenter/hypervisor version ---
    vsource = read_sheet("vSource")
    if not vsource.empty:
        if "Fullname" in vsource.columns and len(vsource) > 0:
            env.vcenter_version = str(vsource["Fullname"].iloc[0])
        if "Version" in vsource.columns and len(vsource) > 0:
            version = str(vsource["Version"].iloc[0])
            env.hypervisor_versions = [f"vSphere {version}"]
            env.aos_versions = [f"vCenter {version}"]
    env.hypervisors = ["VMware vSphere"]

    # --- vSnapshot: snapshot count ---
    vsnapshot = read_sheet("vSnapshot")
    env.snapshots = len(vsnapshot) if not vsnapshot.empty else 0

    # --- vHealth: warnings ---
    vhealth = read_sheet("vHealth")
    if not vhealth.empty and "Message type" in vhealth.columns:
        env.health_warnings = int((vhealth["Message type"].str.lower() != "info").sum())
    elif not vhealth.empty:
        env.health_warnings = len(vhealth)

    # --- Build summary text ---
    parts = []
    if env.clusters:
        parts.append(f"{len(env.clusters)} cluster(s): {', '.join(env.clusters[:3])}")
    if env.total_hosts:
        parts.append(f"{env.total_hosts} ESXi hosts")
    if env.vm_count:
        parts.append(f"{env.vm_count} VMs ({env.powered_on_vms} powered on)")
    if env.total_vcpus:
        parts.append(f"{env.total_vcpus} vCPUs" + (f" ({env.vcpu_overcommit:.1f}x overcommit)" if env.vcpu_overcommit else ""))
    if env.total_ram_gb:
        parts.append(f"{env.total_ram_gb:.0f} GB physical RAM")
    if env.total_storage_tb:
        parts.append(f"{env.total_storage_tb:.1f} TB datastore")
    if env.vcenter_version:
        parts.append(f"vCenter {env.vcenter_version.split()[-1] if env.vcenter_version else ''}")
    if env.snapshots:
        parts.append(f"{env.snapshots} snapshots")

    env.summary_text = "; ".join(parts) if parts else "RVTools data parsed but no standard fields detected."
    return env


@dataclass
class PreCallBrief:
    company_snapshot: str
    contact_intel: str
    pain_points: str
    recent_news: str
    talking_points: list[str]
    questions_to_ask: list[str]
    risk_flags: str
    recommended_angle: str
    environment_summary: str = ""


class PerplexityResearcher:
    """Stage 1: Real-time web research via Perplexity"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.perplexity.ai/chat/completions"

    def research(self, prospect: ProspectInput, env: Optional[NutanixEnvironment] = None) -> str:
        queries = self._build_queries(prospect, env)
        results = []
        for query in queries:
            result = self._query(query)
            results.append(f"### {query}\n{result}")
        return "\n\n".join(results)

    def _build_queries(self, prospect: ProspectInput, env: Optional[NutanixEnvironment]) -> list[str]:
        queries = [
            f"Company overview, business model, size, and revenue for {prospect.company_name}",
            f"Recent news, funding, layoffs, expansions, or strategic shifts at {prospect.company_name} in 2024-2025",
            f"{prospect.contact_name} {prospect.contact_title} at {prospect.company_name} - background, LinkedIn activity, priorities",
            f"{prospect.company_name} technology stack, tools they use, and known pain points or challenges",
            f"{prospect.company_name} competitors and market position in their industry",
        ]
        if env and env.clusters:
            hyp_str = f" running {', '.join(set(env.hypervisors))}" if env.hypervisors else ""
            queries.append(
                f"{prospect.company_name} IT infrastructure strategy, cloud adoption, and data center plans{hyp_str}"
            )
        return queries

    def _query(self, query: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sonar",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a sales research assistant. Be concise, factual, and focus on information useful for a sales call. Include specific data points, numbers, and recent events where available.",
                },
                {"role": "user", "content": query},
            ],
            "max_tokens": 500,
        }
        response = requests.post(self.base_url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


class ClaudeSynthesizer:
    """Stage 2: Synthesize research into structured brief via Claude"""

    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def synthesize(
        self,
        prospect: ProspectInput,
        raw_research: str,
        env: Optional[NutanixEnvironment] = None,
    ) -> PreCallBrief:
        prompt = self._build_prompt(prospect, raw_research, env)

        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            system="""You are an elite sales engineer and coach generating pre-call briefs for Nutanix SE and sales reps.
Your output must be practical, specific, and immediately actionable — not generic fluff.
When environment data is provided, reference it directly with specific numbers.
Always respond with valid JSON and nothing else — no preamble, no markdown, no backticks.""",
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        data = json.loads(text)
        if "environment_summary" not in data:
            data["environment_summary"] = env.summary_text if env else ""

        return PreCallBrief(**data)

    def _build_prompt(
        self,
        prospect: ProspectInput,
        research: str,
        env: Optional[NutanixEnvironment],
    ) -> str:
        segment_context = {
            "commercial": "mid-market commercial account (100-1000 employees), focus on ROI, scalability, and departmental champions",
            "smb": "small-to-medium business (<100 employees), focus on simplicity, cost, time savings, and founder/owner priorities",
        }.get(prospect.segment, prospect.segment)

        env_block = env.to_prompt_block() if env else ""
        has_env = bool(env and env.summary_text)

        env_field = (
            '"environment_summary": "2-3 sentence summary of their current Nutanix environment and what it reveals about their needs",'
            if has_env
            else '"environment_summary": "",'
        )

        env_instruction = (
            "You have real Nutanix Collector data for this account. Reference specific numbers (nodes, VMs, versions, clusters) in your talking points and questions. Identify upgrade opportunities, consolidation plays, or expansion areas based on the environment."
            if has_env
            else "No environment data was provided."
        )

        return f"""
You are preparing a pre-call brief for a Nutanix sales rep about to call:

PROSPECT:
- Company: {prospect.company_name}
- Contact: {prospect.contact_name}, {prospect.contact_title}
- Segment: {segment_context}
- We're selling: {prospect.our_product}
- Rep notes: {prospect.notes or "None"}
{env_block}
WEB RESEARCH:
{research}

INSTRUCTIONS: {env_instruction}

Generate a pre-call brief as JSON with EXACTLY these fields:
{{
  "company_snapshot": "2-3 sentence summary of what the company does, size, and business model",
  "contact_intel": "What we know about this specific person - role, tenure, likely priorities, any public statements",
  "pain_points": "2-3 specific pain points this company faces that our product addresses — use env data if available",
  "recent_news": "Most relevant recent development (funding, expansion, new initiative, leadership change, etc.)",
  {env_field}
  "talking_points": ["3-5 specific talking points — reference actual environment numbers if available"],
  "questions_to_ask": ["4-6 discovery questions to uncover budget/authority/need/timeline — reference their env if available"],
  "risk_flags": "Any red flags: budget constraints, recent bad news, competitive risks, or timing concerns",
  "recommended_angle": "One sentence: the single most compelling angle to lead with on this call"
}}

Be specific. Use real details. Avoid generic sales clichés.
"""


class PreCallAgent:
    """Orchestrates the two-stage research → synthesis pipeline"""

    def __init__(self, anthropic_api_key: str, perplexity_api_key: str):
        self.researcher = PerplexityResearcher(perplexity_api_key)
        self.synthesizer = ClaudeSynthesizer(anthropic_api_key)

    def generate_brief(
        self,
        prospect: ProspectInput,
        env: Optional[NutanixEnvironment] = None,
    ) -> tuple[PreCallBrief, str]:
        print(f"[Stage 1] Researching {prospect.company_name}...")
        raw_research = self.researcher.research(prospect, env)

        print(f"[Stage 2] Synthesizing brief for {prospect.contact_name}...")
        brief = self.synthesizer.synthesize(prospect, raw_research, env)

        return brief, raw_research
