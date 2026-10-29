{% extends 'base.html' %}
{% block title %}Teachers - EIA System{% endblock %}
{% block content %}

<div class="page-header">
  <div>
    <h1><i class="fas fa-chalkboard-teacher"></i> Teachers</h1>
    <p class="form-note">Teachers on this list can be signed out any number of tablets. {{ total }} teacher{{ '' if total == 1 else 's' }} registered.</p>
  </div>
  <div style="display:flex;gap:.6rem;flex-wrap:wrap">
    <a href="{{ url_for('teacher_signout') }}" class="btn btn-primary"><i class="fas fa-hand-holding"></i> Teacher Sign-Out</a>
    <a href="{{ url_for('teacher_checkouts') }}" class="btn btn-outline"><i class="fas fa-clipboard-list"></i> Checkouts</a>
  </div>
</div>

<section class="section">
  <div class="card tr-add">
    <h3 class="tr-h"><i class="fas fa-user-plus"></i> Add teachers</h3>
    <form method="POST" action="{{ url_for('teachers_add') }}">
      <div class="form-group">
        <label for="names">Names (one per line, so you can paste the whole staff list)</label>
        <textarea name="names" id="names" class="form-control" rows="4" required
                  placeholder="Mr. Osojo&#10;Mrs. Namukasa&#10;Ms. Atim"></textarea>
      </div>
      <div class="form-group">
        <label for="phone">Phone (optional, only used when adding a single teacher)</label>
        <input type="text" name="phone" id="phone" class="form-control" placeholder="07xx xxx xxx" maxlength="25"/>
      </div>
      <button class="btn btn-primary" type="submit"><i class="fas fa-plus-circle"></i> Add to register</button>
    </form>
  </div>

  {% if teachers %}
  <div class="card" style="overflow-x:auto;margin-top:1.2rem">
    <table class="data-table">
      <thead>
        <tr><th>Teacher</th><th>Phone</th><th>Tablets out now</th><th>Overdue</th><th>Added by</th><th>Action</th></tr>
      </thead>
      <tbody>
        {% for t in teachers %}
        <tr {% if t['overdue'] %}class="row-overdue"{% endif %}>
          <td><strong>{{ t['full_name'] }}</strong></td>
          <td>{{ t['phone'] or '—' }}</td>
          <td>
            {% if t['holding'] %}<span class="badge badge-yellow">{{ t['holding'] }}</span>
            {% else %}<span class="badge badge-green">0</span>{% endif %}
          </td>
          <td>{% if t['overdue'] %}<span class="badge badge-red">{{ t['overdue'] }} overdue</span>{% else %}—{% endif %}</td>
          <td>{{ t['created_by'] or '—' }}</td>
          <td style="white-space:nowrap">
            <a class="btn btn-sm btn-primary" href="{{ url_for('teacher_checkouts', q=t['full_name']) }}">History</a>
            <form method="POST" action="{{ url_for('teachers_remove', tid=t['id']) }}" style="display:inline"
                  onsubmit="return confirm('Remove {{ t['full_name']|e }} from the register? Past sign-outs are kept.');">
              <button class="btn btn-sm btn-danger" type="submit" {% if t['holding'] %}disabled title="Still has tablets out"{% endif %}>
                <i class="fas fa-trash-alt"></i> Remove
              </button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="empty-state small">
    <i class="fas fa-chalkboard-teacher"></i>
    <p>No teachers yet. Add the staff list above, then sign out tablets to them.</p>
  </div>
  {% endif %}
</section>

<style>
  .tr-add { max-width: 640px; padding: 1.4rem; }
  .tr-h { margin: 0 0 1rem; color: var(--navy); font-size: 1.05rem; }
  .tr-h i { margin-right: .4rem; }
</style>
{% endblock %}
