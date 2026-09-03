(() => {
  "use strict";

  if (location.hash !== "#fte-scan-v1") return;

  const handlers = globalThis.__FTE_MAIN_HANDLERS;
  const captureEcr = () => {
  const value = window.ecrData;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const scalar = (item) => ['string', 'number'].includes(typeof item) ? item : null;
  const clean = (item) => String(item || '').replace(/\s+/g, ' ').trim();
  const visible = (node) => {
    if (!node || !node.isConnected) return false;
    for (let current = node; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) ||
          style.contentVisibility === 'hidden' || Number(style.opacity) === 0) return false;
    }
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const visibleTexts = (selector) => Array.from(document.querySelectorAll(selector))
    .filter(visible).map((element) => clean(element.innerText)).filter(Boolean);
  const exactlyOne = (values) => values.length === 1 ? values[0] : null;
  const settings = typeof _appSettings !== 'undefined' && _appSettings &&
    typeof _appSettings === 'object' && !Array.isArray(_appSettings) ? _appSettings : null;
  const canonicalLinks = Array.from(document.querySelectorAll('link[rel~="canonical"]'));
  let canonical = null;
  if (canonicalLinks.length === 1) {
    try { canonical = new URL(canonicalLinks[0].href); } catch (_) { canonical = null; }
  }
  const headings = visibleTexts('h1.rankings-page__heading');
  const periods = visibleTexts(
    '.select-advanced--ranking .select-advanced__button-text'
  );
  const fallbackNotes = visibleTexts(
    'section.ecr-coach-prompts[role="region"] > p.ecr-coach-prompts__note'
  );
  const expertIds = String(value.filters || '').split(',').map((item) => item.trim())
    .filter((item) => /^[1-9]\d{0,19}$/.test(item));
  const groups = typeof expertGroupsData !== 'undefined' && expertGroupsData &&
    typeof expertGroupsData === 'object' && !Array.isArray(expertGroupsData) ?
    expertGroupsData : null;
  const defaultGroup = groups && groups.expert_groups && groups.expert_groups.default;
  const defaultOptions = defaultGroup && Array.isArray(defaultGroup.options) ?
    defaultGroup.options : [];
  const latestOptions = defaultOptions.filter((option) => option &&
    typeof option === 'object' && !Array.isArray(option) && option.id === 'default');
  const latest = latestOptions.length === 1 ? latestOptions[0] : null;
  const latestExpertIds = latest && Array.isArray(latest.experts) ? latest.experts
    .map((item) => scalar(item)).filter((item) => item !== null).map(String) : [];
  const rawPlayers = Array.isArray(value.players) ? value.players : [];
  if (rawPlayers.length > 5000) return {error: 'too_many_rankings'};
  const positionCounts = {};
  rawPlayers.forEach((player) => {
    const position = player && scalar(player.player_position_id);
    if (position !== null) positionCounts[String(position)] =
      (positionCounts[String(position)] || 0) + 1;
  });
  const rankings = rawPlayers.map((player) => {
    if (!player || typeof player !== 'object' || Array.isArray(player)) return null;
    return {
      player_id: scalar(player.player_id),
      player_name: scalar(player.player_name),
      team: scalar(player.player_team_id),
      position: scalar(player.player_position_id),
      rank_ecr: scalar(player.rank_ecr),
      rank_min: scalar(player.rank_min),
      rank_max: scalar(player.rank_max),
      rank_avg: scalar(player.rank_ave),
      rank_std: scalar(player.rank_std),
      position_rank: scalar(player.pos_rank)
    };
  }).filter(Boolean);
  return {
    source: {
      sport: scalar(value.sport),
      ranking_type: scalar(value.ranking_type_name),
      type_text: scalar(value.type),
      year: scalar(value.year),
      week: scalar(value.week),
      position: scalar(value.position_id),
      scoring: scalar(value.scoring),
      expert_ids: expertIds,
      expert_count: scalar(value.total_experts),
      expert_policy: {
        policy_id: 'fantasypros_latest_ecr_v1',
        group_id: latest && scalar(latest.id),
        title: latest && scalar(latest.title),
        description: latest && scalar(latest.description),
        expert_ids: latestExpertIds
      },
      last_updated: scalar(value.last_updated),
      last_updated_ts: scalar(value.last_updated_ts),
      player_count: scalar(value.count),
      position_counts: positionCounts,
      page_evidence: {
        protocol: location.protocol,
        hostname: location.hostname.toLowerCase(),
        port: location.port,
        pathname: location.pathname,
        canonical_protocol: canonical && canonical.protocol,
        canonical_hostname: canonical && canonical.hostname.toLowerCase(),
        canonical_port: canonical && canonical.port,
        canonical_pathname: canonical && canonical.pathname,
        canonical_link_count: canonicalLinks.length,
        document_title: document.title,
        settings_ranking_type: settings && scalar(settings.ranking_type_name),
        settings_position: settings && scalar(settings.position_data),
        settings_page_heading: settings && scalar(settings.page_heading),
        settings_fallback_note: settings && scalar(settings.ros_fallback_note),
        visible_page_heading: exactlyOne(headings),
        visible_page_heading_count: headings.length,
        visible_ranking_period: exactlyOne(periods),
        visible_ranking_period_count: periods.length,
        visible_fallback_note: exactlyOne(fallbackNotes),
        visible_fallback_note_count: fallbackNotes.length
      }
    },
    rankings
  };
};

  handlers["ecr.capture"] = () => captureEcr();
})();
