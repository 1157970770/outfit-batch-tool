import type { UserRecord } from '../config/app-config.service';

export interface AuthUser {
  username: string;
  role: string;
  record: UserRecord;
}
