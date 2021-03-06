import PropTypes from 'prop-types';
import React from 'react';

import {addSuccessMessage} from '../../actionCreators/indicator';
import {t, tct} from '../../locale';
import Form from '../../views/settings/components/forms/form';
import SentryTypes from '../../proptypes';
import TextField from '../../views/settings/components/forms/textField';

export default class CreateTeamForm extends React.Component {
  static propTypes = {
    organization: SentryTypes.Organization.isRequired,
    onSuccess: PropTypes.func.isRequired,
    onSubmit: PropTypes.func,
  };

  handleCreateTeamSuccess = data => {
    addSuccessMessage(tct('Added team [team]', {team: `#${data.slug}`}));
    this.props.onSuccess(data);
  };

  render() {
    let {organization} = this.props;
    let features = new Set(organization.features);

    return (
      <React.Fragment>
        <p>
          {t(
            "Teams group members' access to a specific focus, e.g. a major product or application that may have sub-projects."
          )}
        </p>

        <Form
          submitLabel={t('Create Team')}
          apiEndpoint={`/organizations/${organization.slug}/teams/`}
          apiMethod="POST"
          onSubmit={this.props.onSubmit}
          onSubmitSuccess={this.handleCreateTeamSuccess}
          requireChanges
        >
          {features.has('new-teams') ? (
            <TextField
              name="slug"
              label={t('Team Slug')}
              placeholder={t('e.g. operations, web-frontend, desktop')}
              help={t('May contain letters, numbers, dashes and underscores.')}
              required
              flexibleControlStateSize
              inline={false}
              p={0}
            />
          ) : (
            <TextField
              name="name"
              label={t('Team Name')}
              placeholder={t('e.g. Operations, Web, Desktop')}
              required
              flexibleControlStateSize
              inline={false}
              p={0}
            />
          )}
        </Form>
      </React.Fragment>
    );
  }
}
