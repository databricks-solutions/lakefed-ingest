"""The Needs attention card, as the cards board approved it: the title is plain, the headline is the job count and opens
the Ingestion jobs list filtered to attention jobs (or the job when there is only one), and the note sits behind the
info icon. The attention filter is one rule shared by the Overview list and the list's filter. Plain source checks, as
the other frontend tests are."""
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src"
SOURCE = APP / "frontend" / "src"
OVERVIEW = (SOURCE / "pages" / "Overview.tsx").read_text()
JOBS_PAGE = (SOURCE / "pages" / "IngestionJobs.tsx").read_text()
INGESTION_JOBS = (APP / "ingestion_jobs.py").read_text()
MAIN = (APP / "main.py").read_text()


def _function(source, name):
    start = source.index(f"export function {name}(")
    return source[start:source.index("\n}\n", start)]


def test_needs_attention_title_is_plain_and_the_count_is_the_headline():
    card = _function(OVERVIEW, "NeedsAttention")
    assert "<Section title={<>Needs attention <Info>" in card
    assert "Needs attention: " not in card and "note=" not in card


def test_needs_attention_headline_links_to_the_attention_filter_or_the_one_job():
    card = _function(OVERVIEW, "NeedsAttention")
    assert 'const attentionTo = total === 1 ? jobPath(jobs[0].id) : "/ingestion-jobs?filter=attention";' in card
    assert "<Link to={attentionTo}>" in card


def test_needs_attention_has_no_causes_list_and_no_all_link():
    card = _function(OVERVIEW, "NeedsAttention")
    assert "causes" not in card and "All {fmtInt(total)}" not in card


def test_the_ingestion_jobs_list_has_a_needs_attention_chip_on_the_attention_filter():
    assert '["attention", "Needs attention"]' in JOBS_PAGE


def test_the_attention_filter_uses_the_same_rule_as_the_overview_list():
    assert "def needs_attention(job: dict) -> bool:" in INGESTION_JOBS
    assert "attention = [j for j in rows if needs_attention(j)]" in INGESTION_JOBS
    assert '"attention")' in MAIN and '"attention": ij.needs_attention(job)' in MAIN
