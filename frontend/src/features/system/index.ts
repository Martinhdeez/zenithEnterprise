/** Administration above every tenant. */
export { System } from "./System";
export {
  organisations,
  provisionOrganisation,
  suspendOrganisation,
  activateOrganisation,
  purgeOrganisation,
  type Organisation,
  type OrganisationStatus,
  type Provisioned,
} from "./api";
