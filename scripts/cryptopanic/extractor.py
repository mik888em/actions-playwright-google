"""JS-экстрактор карточек новостей CryptoPanic."""

EXTRACT_JS = r"""
() => {
  function abs(u){ try { return new URL(u, location.origin).href } catch(e){ return u || '' } }
  function isoUTCFromAttr(el){
    try{
      if(!el) return "";
      const raw = el.getAttribute("datetime") || "";
      if(!raw) return "";
      const d = new Date(raw);
      if (isNaN(d)) return "";
      return d.toISOString().replace(/\.\d{3}Z$/, "Z");
    } catch(e){ return "" }
  }
  function extractCoins(row){
    const zone = row.querySelector(".news-cell.nc-currency");
    if(!zone) return "---";
    const coins = [];
    zone.querySelectorAll("a[href]").forEach(a=>{
      const h = a.getAttribute("href") || "";
      const m = h.match(/\/news\/([^\/]+)\//i);
      if(m){
        let tick = (a.textContent || "").trim();
        if(tick && tick[0] !== "$") tick = "$" + tick + " ";
        coins.push({ coin: m[1], tick });
      }
    });
    return coins.length ? coins : "---";
  }
  function extractVotes(row){
    const votes = {comments:0, likes:0, dislikes:0, lol:0, save:0, important:0, negative:0, neutral:0};
    row.querySelectorAll("[title]").forEach(el=>{
      const t = (el.getAttribute("title") || "").toLowerCase();
      const m = t.match(/(\d+)\s+(comments?|like|dislike|lol|save|important|negative|neutral)\s+votes?/i);
      if(m){
        const n = parseInt(m[1],10);
        const key = m[2];
        if(/comment/.test(key)) votes.comments = n;
        else if(key==="like") votes.likes = n;
        else if(key==="dislike") votes.dislikes = n;
        else if(key==="lol") votes.lol = n;
        else if(key==="save") votes.save = n;
        else if(key==="important") votes.important = n;
        else if(key==="negative") votes.negative = n;
        else if(key==="neutral") votes.neutral = n;
      }
    });
    return votes;
  }

  const out = [];
  const rows = document.querySelectorAll("div.news-row.news-row-link");
  rows.forEach(row => {
    const aTitle = row.querySelector("a.news-cell.nc-title");
    const aDate  = row.querySelector("a.news-cell.nc-date");
    const href   = (aTitle?.getAttribute("href") || aDate?.getAttribute("href") || "").trim();
    const timeEl = row.querySelector("a.news-cell.nc-date time");
    const time_iso_utc = isoUTCFromAttr(timeEl);
    const time_rel = (timeEl?.textContent || "").trim();
    const title = (row.querySelector(".nc-title .title-text span")?.textContent || "").trim();
    const source = (row.querySelector(".si-source-domain")?.textContent || "").trim();
    const idm = href.match(/\/news\/(\d+)/);
    const id_news = idm ? idm[1] : "";

    if (href) {
      out.push({
        url_rel: href,
        url_abs: abs(href),
        time_iso: time_iso_utc,
        time_rel,
        title,
        source,
        id_news,
        coins: extractCoins(row),
        votes: extractVotes(row)
      });
    }
  });
  return out;
}
"""

__all__ = ["EXTRACT_JS"]
