import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';

import { Config } from '../models';
import { ApiService } from './api.service';
import { OpenInService, encodeRelativePath } from './open-in.service';

const CONFIG: Config = {
  root_dir: '/footage',
  task_poll_interval_ms: 1000,
  browser_hidden_extensions: [],
  google_maps_api_key: '',
  google_maps_map_id: '',
  google_maps_map_id_poi: '',
  trash_dir_name: '.trash',
};

describe('encodeRelativePath', () => {
  it('encodes a plain path under rootDir', () => {
    expect(encodeRelativePath('/footage/japan_2024/photo/P1.jpg', '/footage'))
      .toBe('japan_2024/photo/P1.jpg');
  });

  it('percent-encodes a segment with a space and Japanese characters', () => {
    expect(encodeRelativePath('/footage/熱海 atami/P1.jpg', '/footage'))
      .toBe('%E7%86%B1%E6%B5%B7%20atami/P1.jpg');
  });

  it('accepts a rootDir with a trailing slash', () => {
    expect(encodeRelativePath('/footage/japan/P1.jpg', '/footage/'))
      .toBe('japan/P1.jpg');
  });

  it('returns null for a path outside rootDir', () => {
    expect(encodeRelativePath('/other/japan/P1.jpg', '/footage')).toBeNull();
  });

  it('returns null for a prefix match that is not on a segment boundary', () => {
    expect(encodeRelativePath('/footage/japan/x.jpg', '/footage/jap')).toBeNull();
  });

  it('returns null for rootDir itself', () => {
    expect(encodeRelativePath('/footage', '/footage')).toBeNull();
  });
});

describe('OpenInService', () => {
  let service: OpenInService;

  beforeEach(() => {
    try {
      localStorage.removeItem('fa-open-in-enabled');
    } catch {
      /* ignore */
    }

    TestBed.configureTestingModule({
      providers: [
        { provide: ApiService, useValue: { getConfig: () => of(CONFIG) } },
      ],
    });
    service = TestBed.inject(OpenInService);
  });

  it('appsFor returns [] when disabled', () => {
    expect(service.appsFor('.jpg')).toEqual([]);
  });

  it('appsFor matches a mixed-case extension with a leading dot when enabled', () => {
    service.setEnabled(true);
    expect(service.appsFor('.RW2').map(app => app.id)).toEqual(['photoshop']);
    expect(service.appsFor('rw2').map(app => app.id)).toEqual(['photoshop']);
  });

  it('appsFor returns [] for an unsupported extension', () => {
    service.setEnabled(true);
    expect(service.appsFor('.txt')).toEqual([]);
  });
});
