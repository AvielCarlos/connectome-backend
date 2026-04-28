import React, { useEffect, useState } from 'react';
import {
  View,
  Text,
  StyleSheet,
  ActivityIndicator,
  TouchableOpacity,
  SafeAreaView,
  Alert,
} from 'react-native';
import { getProfile, UserProfile } from '../lib/api';
import { clearToken } from '../lib/auth';

type Props = {
  onSignOut: () => void;
};

const TIER_COLORS: Record<string, string> = {
  free: '#555',
  pro: '#8b5cf6',
  premium: '#f59e0b',
};

export default function ProfileScreen({ onSignOut }: Props) {
  const [profile, setProfile] = useState<UserProfile | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      try {
        const p = await getProfile();
        setProfile(p);
      } catch {
        // silent — show empty state
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const handleSignOut = () => {
    Alert.alert('Sign out', 'Are you sure you want to sign out?', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Sign out',
        style: 'destructive',
        onPress: async () => {
          await clearToken();
          onSignOut();
        },
      },
    ]);
  };

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" color="#8b5cf6" />
      </View>
    );
  }

  const tier = profile?.tier ?? 'free';
  const tierColor = TIER_COLORS[tier] ?? '#555';

  return (
    <SafeAreaView style={styles.container}>
      <View style={styles.header}>
        <View style={styles.avatar}>
          <Text style={styles.avatarText}>
            {profile?.name ? profile.name[0].toUpperCase() : '?'}
          </Text>
        </View>
        <Text style={styles.name}>{profile?.name ?? '—'}</Text>
        <Text style={styles.email}>{profile?.email ?? '—'}</Text>
        <View style={[styles.tierBadge, { borderColor: tierColor }]}>
          <Text style={[styles.tierText, { color: tierColor }]}>
            {tier.toUpperCase()}
          </Text>
        </View>
      </View>

      <View style={styles.stats}>
        <View style={styles.statCard}>
          <Text style={styles.statNumber}>
            {profile?.goals_count ?? (profile?.goals?.length ?? 0)}
          </Text>
          <Text style={styles.statLabel}>Goals</Text>
        </View>
        <View style={styles.statDivider} />
        <View style={styles.statCard}>
          <Text style={styles.statNumber}>{profile?.interactions_count ?? 0}</Text>
          <Text style={styles.statLabel}>Interactions</Text>
        </View>
      </View>

      <TouchableOpacity style={styles.signOutBtn} onPress={handleSignOut}>
        <Text style={styles.signOutText}>Sign Out</Text>
      </TouchableOpacity>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#0a0a0a' },
  center: {
    flex: 1,
    backgroundColor: '#0a0a0a',
    justifyContent: 'center',
    alignItems: 'center',
  },
  header: {
    alignItems: 'center',
    paddingTop: 40,
    paddingBottom: 32,
    paddingHorizontal: 24,
    borderBottomWidth: 1,
    borderBottomColor: '#1a1a1a',
  },
  avatar: {
    width: 80,
    height: 80,
    borderRadius: 40,
    backgroundColor: '#8b5cf620',
    borderWidth: 2,
    borderColor: '#8b5cf6',
    justifyContent: 'center',
    alignItems: 'center',
    marginBottom: 16,
  },
  avatarText: { color: '#8b5cf6', fontSize: 32, fontWeight: '700' },
  name: { color: '#fff', fontSize: 22, fontWeight: '700', marginBottom: 4 },
  email: { color: '#666', fontSize: 14, marginBottom: 12 },
  tierBadge: {
    borderRadius: 20,
    borderWidth: 1,
    paddingHorizontal: 14,
    paddingVertical: 4,
  },
  tierText: { fontSize: 11, fontWeight: '700', letterSpacing: 1 },
  stats: {
    flexDirection: 'row',
    margin: 24,
    backgroundColor: '#1a1a1a',
    borderRadius: 16,
    borderWidth: 1,
    borderColor: '#2a2a2a',
    overflow: 'hidden',
  },
  statCard: {
    flex: 1,
    alignItems: 'center',
    paddingVertical: 20,
  },
  statDivider: { width: 1, backgroundColor: '#2a2a2a' },
  statNumber: { color: '#fff', fontSize: 28, fontWeight: '700', marginBottom: 4 },
  statLabel: { color: '#666', fontSize: 13 },
  signOutBtn: {
    marginHorizontal: 24,
    marginTop: 'auto',
    marginBottom: 32,
    backgroundColor: '#1a1a1a',
    borderRadius: 12,
    paddingVertical: 15,
    alignItems: 'center',
    borderWidth: 1,
    borderColor: '#ef44441a',
  },
  signOutText: { color: '#ef4444', fontSize: 16, fontWeight: '600' },
});
