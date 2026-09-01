import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import type { RequirementsGraphFile } from './graph.types';

@Injectable({ providedIn: 'root' })
export class GraphDataService {
  private readonly http = inject(HttpClient);

  load() {
    return this.http.get<RequirementsGraphFile>('assets/requirements-graph.json');
  }
}
