// @flow
// health http api module
import {createSelector, type Selector} from 'reselect'

import type {State, ThunkPromiseAction} from '../types'
import type {BaseRobot, RobotService} from '../robot'
import type {ApiCall, ApiRequestError} from './types'
import type {ApiAction} from './actions'

import {apiRequest, apiSuccess, apiFailure} from './actions'
import {getRobotApiState} from './reducer'
import client from './client'

type FetchHealthResponse = {
  name: string,
  api_version: string,
  fw_version: string,
  logs: ?Array<string>,
}

export type HealthAction =
  | ApiAction<'health', null, FetchHealthResponse>

export type FetchHealthCall = ApiCall<void, FetchHealthResponse>

export type RobotHealthState = {
  health?: FetchHealthCall,
}

const HEALTH: 'health' = 'health'

export function fetchHealth (robot: RobotService): ThunkPromiseAction {
  return (dispatch) => {
    dispatch(apiRequest(robot, HEALTH, null))

    return client(robot, 'GET', HEALTH)
      .then(
        (resp: FetchHealthResponse) => apiSuccess(robot, HEALTH, resp),
        (err: ApiRequestError) => apiFailure(robot, HEALTH, err)
      )
      .then(dispatch)
  }
}

export const makeGetRobotHealth = () => {
  const selector: Selector<State, BaseRobot, FetchHealthCall> = createSelector(
    getRobotApiState,
    state => state[HEALTH] || {inProgress: false}
  )

  return selector
}
