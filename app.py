"""
Flask web app for Pre-Call Intelligence Agent
"""

import os
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template
from agent import PreCallAgent, ProspectInput, parse_nutanix_csv, parse_rvtools_xlsx

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB max upload


def get_agent():
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    perplexity_key = os.environ.get("PERPLEXITY_API_KEY")
    if not anthropic_key or not perplexity_key:
        raise ValueError("Missing API keys: set ANTHROPIC_API_KEY and PERPLEXITY_API_KEY")
    return PreCallAgent(anthropic_key, perplexity_key)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    # Support both multipart/form-data (with file) and application/json
    if request.content_type and "application/json" in request.content_type:
        data = request.get_json()
        get_field = lambda k: data.get(k, "")
        csv_file = None
    else:
        get_field = lambda k: request.form.get(k, "")
        csv_file = request.files.get("collector_csv")

    required = ["company_name", "contact_name", "contact_title", "segment", "our_product"]
    for field_name in required:
        if not get_field(field_name):
            return jsonify({"error": f"Missing field: {field_name}"}), 400

    prospect = ProspectInput(
        company_name=get_field("company_name"),
        contact_name=get_field("contact_name"),
        contact_title=get_field("contact_title"),
        segment=get_field("segment"),
        our_product=get_field("our_product"),
        notes=get_field("notes"),
    )

    # Optional file — read into memory, never saved to disk
    env = None
    if csv_file and csv_file.filename:
        file_bytes = csv_file.read()
        csv_file.close()
        fname = csv_file.filename.lower()
        if fname.endswith(".xlsx") or fname.endswith(".xls"):
            env = parse_rvtools_xlsx(file_bytes)
        else:
            env = parse_nutanix_csv(file_bytes)
        del file_bytes

    try:
        agent = get_agent()
        brief, raw_research = agent.generate_brief(prospect, env)

        return jsonify({
            "brief": {
                "company_snapshot": brief.company_snapshot,
                "contact_intel": brief.contact_intel,
                "pain_points": brief.pain_points,
                "recent_news": brief.recent_news,
                "environment_summary": brief.environment_summary,
                "talking_points": brief.talking_points,
                "questions_to_ask": brief.questions_to_ask,
                "risk_flags": brief.risk_flags,
                "recommended_angle": brief.recommended_angle,
            },
            "env_parsed": {
                "source_type": env.source_type,
                "clusters": env.clusters,
                "total_nodes": env.total_nodes,
                "total_hosts": env.total_hosts,
                "total_vms": env.vm_count,
                "powered_on_vms": env.powered_on_vms,
                "total_vcpus": env.total_vcpus,
                "total_ram_gb": env.total_ram_gb,
                "total_storage_tb": env.total_storage_tb,
                "hypervisors": env.hypervisors,
                "hypervisor_versions": env.hypervisor_versions,
                "aos_versions": env.aos_versions,
                "workload_types": env.workload_types,
                "os_types": env.os_types,
                "host_models": env.host_models,
                "snapshots": env.snapshots,
                "health_warnings": env.health_warnings,
                "vcenter_version": env.vcenter_version,
                "vcpu_overcommit": env.vcpu_overcommit,
                "vram_overcommit": env.vram_overcommit,
                "summary": env.summary_text,
            } if env else None,
            "raw_research": raw_research,
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Pipeline error: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)