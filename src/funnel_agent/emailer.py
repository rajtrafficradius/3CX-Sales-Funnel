"""Lisa's EMAIL channel — a SECONDARY reinforcement to SMS.

SMS is always the PRIMARY outbound channel (Vysakh's rule: "give priority to SMS communication plus email if
available"). Whenever we ALSO have a prospect's email on file, we send a matching, on-brand HTML email from
Lisa's real mailbox (lisa@trafficradius.com.au) via Microsoft Graph (Mail.Send — confirmed granted on this
tenant). Fully guarded — never raises; returns False if we have no address, Graph isn't configured, or the
send fails, so it can be bolted onto any SMS path without risk.
"""
import os
import re as _re
import html as _h

# Prospect-facing email MUST come from the Digital Expo / DE Group brand domain — NEVER trafficradius.com.au
# (Vysakh, firm). Set env LISA_MAILBOX to the exact provisioned digitalexpo address to override.
DEFAULT_MAILBOX = "lisa@digitalexpo.com.au"
_BRAND_DOMAIN = "digitalexpo.com.au"


def lisa_mailbox(settings) -> str:
    return (getattr(settings, "lisa_mailbox", "") or os.getenv("LISA_MAILBOX", "") or DEFAULT_MAILBOX).strip()


def prospect_email(pool, dest9: str) -> str:
    """Best email we hold for a prospect, tried in order of trust. '' if none. Guarded per-source so a
    missing column (e.g. no email on lisa4_pool) simply falls through."""
    from . import lisa as _l
    d9 = _re.sub(r"[^0-9]", "", dest9 or "")[-9:]
    if not d9:
        return ""
    for sql in (
        "SELECT contact_email e FROM booked_crm WHERE dest9=%s AND COALESCE(contact_email,'')<>''",
        "SELECT prospect_email e FROM lisa_calls WHERE dest9=%s AND COALESCE(prospect_email,'')<>'' "
        "  ORDER BY started_at DESC LIMIT 1",
        "SELECT email e FROM lisa5_pool WHERE dest9=%s AND COALESCE(email,'')<>''",
        "SELECT email e FROM lisa4_pool WHERE dest9=%s AND COALESCE(email,'')<>''",
    ):
        try:
            r = _l._fetch(pool, sql, (d9,))
            if r:
                e = (r[0].get("e") or "").strip()
                if "@" in e and "." in e.split("@")[-1] and " " not in e:
                    return e
        except Exception:
            pass
    return ""


def send_email(settings, to_email: str, subject: str, html: str, dest9: str | None = None) -> bool:
    """Send an HTML email FROM Lisa's mailbox via Graph. True only if it actually went out. Guarded.
    When dest9 is given, an invisible open-pixel is injected so we can see if the prospect opened it."""
    to = (to_email or "").strip()
    if not to or "@" not in to:
        return False
    # QA G11: scrub banned phrases (e.g. "video link") from the subject + body before it leaves. Pure
    # CPU; no URL rewriting here (would touch HTML href attributes). Guarded — never blocks the send.
    try:
        from .qa import outbound as _qa_out
        subject = _qa_out.scrub_phrases(subject)[0]
        html = _qa_out.scrub_phrases(html)[0]
    except Exception:
        pass
    try:
        if not getattr(settings, "graph_configured", False):
            return False
        box = lisa_mailbox(settings)
        if not box.lower().endswith("@" + _BRAND_DOMAIN):
            return False   # prospect email only ever goes from the Digital Expo brand domain (never trafficradius)
        if dest9:
            try:
                from . import tracking as _trk
                px = _trk.email_pixel(dest9, "https://www.trmatrix.com.au")
                if px:
                    low = html.lower()
                    k = low.rfind("</body>")
                    html = (html[:k] + px + html[k:]) if k >= 0 else (html + px)
            except Exception:
                pass
        from . import emma as _emma
        _emma._graph(settings, "POST", f"/users/{lisa_mailbox(settings)}/sendMail", {
            "message": {"subject": subject[:220],
                        "body": {"contentType": "HTML", "content": html},
                        "toRecipients": [{"emailAddress": {"address": to}}]},
            "saveToSentItems": True})
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# on-brand HTML (table-based for email-client compatibility)
# --------------------------------------------------------------------------- #
def _shell(preheader: str, inner: str) -> str:
    return f'''<!doctype html><html><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/></head>
<body style="margin:0;padding:0;background:#eef3f9;">
<span style="display:none;max-height:0;overflow:hidden;opacity:0">{_h.escape(preheader)}</span>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3f9;padding:24px 12px">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;box-shadow:0 12px 34px -18px rgba(11,21,36,.35);font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif">
  <tr><td style="background:linear-gradient(135deg,#0a1930,#12294a);padding:22px 26px">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="font-size:18px;font-weight:800;color:#fff;font-family:-apple-system,Segoe UI,Roboto,Arial">Digital&nbsp;Expo
        <span style="display:block;font-size:9px;letter-spacing:2px;color:#9fb2cc;font-weight:800">DE GROUP</span></td>
    </tr></table>
  </td></tr>
  <tr><td style="padding:26px 26px 8px;color:#0b1524;font-size:15.5px;line-height:1.6">{inner}</td></tr>
  <tr><td style="padding:16px 26px 24px;color:#8595a9;font-size:12px;line-height:1.5;border-top:1px solid #eef2f7">
    Digital Expo · DE Group — Google Partner digital marketing agency<br/>
    (03) 7020 9196 · digitalexpo.com.au · ABN 90 134 920 228<br/>
    <span style="color:#aeb9c9">Prefer not to hear from us? Just reply “stop”.</span>
  </td></tr>
</table></td></tr></table></body></html>'''


def _btn(url: str, label: str) -> str:
    return (f'<a href="{_h.escape(url)}" style="display:inline-block;background:#1f5fd0;color:#ffffff;'
            f'text-decoration:none;font-weight:700;padding:12px 24px;border-radius:9px;font-size:15px">'
            f'{_h.escape(label)}</a>')


def _hi(name: str) -> str:
    return f"Hi {_h.escape(name)}," if name else "Hi there,"


def brand_intro_email(name: str, brand_url: str, site_url: str | None = None) -> tuple[str, str]:
    site = (f'<p style="margin:0 0 16px">While you\'re there, here\'s the new website we\'ve put together '
            f'for you as well:</p><p style="margin:0 0 20px">{_btn(site_url, "View your new website")}</p>') if site_url else ""
    inner = (f'<p style="margin:0 0 14px">{_hi(name)}</p>'
             f'<p style="margin:0 0 16px">Ahead of our chat, here\'s a quick introduction to who we are at '
             f'DE Group and the kind of results we get for local businesses.</p>'
             f'<p style="margin:0 0 20px">{_btn(brand_url, "Read the DE Group intro")}</p>'
             f'{site}'
             f'<p style="margin:0 0 6px">Looking forward to showing you what we\'ve built.</p>'
             f'<p style="margin:0">Cheers,<br/><b>Lisa</b> · DE Group</p>')
    return "A quick intro to DE Group", _shell("A quick intro to who we are at DE Group.", inner)


def reveal_email(name: str, company: str, site_url: str, brand_url: str,
                 compare_url: str | None = None, *, recovery: bool = False) -> tuple[str, str]:
    co = _h.escape(company or "your business")
    cmp_html = (f'<p style="margin:0 0 12px">And here\'s a quick before/after against your current site:</p>'
                f'<p style="margin:0 0 20px">{_btn(compare_url, "See the before / after")}</p>') if compare_url else ""
    lead = (f'We had a time set to walk you through this and missed you — no worries at all. '
            f'Rather than wait, here\'s the new website we built for {co}, ready to view now:'
            if recovery else
            f'As promised, here\'s the new website we built for {co} — have a look whenever suits:')
    inner = (f'<p style="margin:0 0 14px">{_hi(name)}</p>'
             f'<p style="margin:0 0 16px">{lead}</p>'
             f'<p style="margin:0 0 20px">{_btn(site_url, "View your new website")}</p>'
             f'{cmp_html}'
             f'<p style="margin:0 0 16px">A quick intro to us is here too: '
             f'<a href="{_h.escape(brand_url)}" style="color:#1f5fd0;font-weight:600">who we are at DE Group</a>.</p>'
             f'<p style="margin:0 0 6px">Happy to jump on a quick call and walk you through it — just reply with '
             f'a time that suits and I\'ll lock it in.</p>'
             f'<p style="margin:0">Cheers,<br/><b>Lisa</b> · DE Group</p>')
    subject = (f"The website we built for {company}" if not recovery
               else f"Your new website is ready to view, {name}".rstrip(", ")) if company else "Your new website"
    return subject, _shell("Your new website is ready to view.", inner)
