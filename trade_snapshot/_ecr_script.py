"""Finite allowlisted FantasyPros ECR bootstrap extraction."""


ECR_BOOTSTRAP_SCRIPT = r"""
() => {
  const value = window.ecrData;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const scalar = (item) => ['string', 'number'].includes(typeof item) ? item : null;
  const expertIds = String(value.filters || '').split(',').map((item) => item.trim())
    .filter((item) => /^[1-9]\d{0,19}$/.test(item));
  const rawPlayers = Array.isArray(value.players) ? value.players : [];
  if (rawPlayers.length > 5000) return {error: 'too_many_rankings'};
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
      last_updated: scalar(value.last_updated),
      player_count: scalar(value.count)
    },
    rankings
  };
}
"""


__all__ = ("ECR_BOOTSTRAP_SCRIPT",)
